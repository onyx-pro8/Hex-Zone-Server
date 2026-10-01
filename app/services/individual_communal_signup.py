"""Individual (Exclusive) signup: subscribe to a public Communal ID as primary.

Solo Individuals (self sign-up or system-admin QR) select an existing Communal ID.
All zones tagged with that ID are calculated into **one** owned primary zone.
They may then create up to ``INDIVIDUAL_SECONDARY_ZONE_LIMIT`` secondary zones.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.owner import AccountType
from app.models.zone import Zone, ZoneType, geojson_to_wkt
from app.services.communal_zone_service import (
    count_zones_with_communal_id,
    find_zones_by_communal_id,
    is_valid_reference_format,
    normalize_reference_id,
    registry_communal_id_taken,
    zone_config,
    zone_parameters,
)


def assert_exclusive_communal_id_selectable(db: Session, raw: str | None) -> str:
    """Validate a Communal ID for Individual signup. Must be registered and have zones."""
    normalized = normalize_reference_id(raw or "")
    if not normalized or not is_valid_reference_format(normalized):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "COMMUNAL_ID_REQUIRED",
                "message": (
                    "Individual accounts must select a valid Communal ID "
                    "(3–32 characters, letters/numbers/_/-)."
                ),
            },
        )
    if not registry_communal_id_taken(db, normalized):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "COMMUNAL_ID_NOT_FOUND",
                "message": f"Communal ID {normalized} is not registered.",
            },
        )
    if count_zones_with_communal_id(db, normalized) <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "COMMUNAL_ID_EMPTY",
                "message": (
                    f"Communal ID {normalized} has no zones yet. "
                    "Choose a Communal ID that already has zones attached."
                ),
            },
        )
    return normalized


def _polygon_parts_from_geojson(geom: Any) -> list[list[list[list[float]]]]:
    if not isinstance(geom, dict):
        return []
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Polygon" and isinstance(coords, list):
        return [coords]  # type: ignore[list-item]
    if gtype == "MultiPolygon" and isinstance(coords, list):
        return [part for part in coords if isinstance(part, list)]
    return []


def _source_geojson(zone: Zone) -> dict[str, Any] | None:
    geom = getattr(zone, "geo_fence_polygon", None)
    if isinstance(geom, dict) and geom.get("type") in {"Polygon", "MultiPolygon"}:
        return geom
    params = zone_parameters(zone)
    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    nested = geometry.get("geo_fence_polygon") if isinstance(geometry, dict) else None
    if isinstance(nested, dict) and nested.get("type") in {"Polygon", "MultiPolygon"}:
        return nested
    return None


def _merge_source_geometries(sources: list[Zone]) -> tuple[dict[str, Any] | None, list[str]]:
    """Union polygon parts + unique H3 cells from all Communal-tagged zones."""
    polygon_parts: list[list[list[list[float]]]] = []
    h3_cells: list[str] = []
    seen_cells: set[str] = set()
    for zone in sources:
        geo = _source_geojson(zone)
        if geo is not None:
            polygon_parts.extend(_polygon_parts_from_geojson(geo))
        for cell in list(getattr(zone, "h3_cells", None) or []):
            text = str(cell or "").strip()
            if text and text not in seen_cells:
                seen_cells.add(text)
                h3_cells.append(text)
        config = zone_config(zone)
        for key in ("h3_cells", "h3Cells"):
            raw = config.get(key)
            if not isinstance(raw, list):
                continue
            for cell in raw:
                text = str(cell or "").strip()
                if text and text not in seen_cells:
                    seen_cells.add(text)
                    h3_cells.append(text)
    multipoly = (
        {"type": "MultiPolygon", "coordinates": polygon_parts} if polygon_parts else None
    )
    return multipoly, h3_cells


def provision_individual_primary_from_communal(
    db: Session,
    owner,
    communal_id: str,
) -> Zone:
    """Create one owned primary zone calculated from all zones under the Communal ID."""
    normalized = normalize_reference_id(communal_id)
    sources = find_zones_by_communal_id(db, normalized)
    if not sources:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "COMMUNAL_ID_EMPTY",
                "message": f"Communal ID {normalized} has no zones yet.",
            },
        )

    multipoly, h3_cells = _merge_source_geometries(sources)
    if multipoly is None and not h3_cells:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "COMMUNAL_ID_NO_GEOMETRY",
                "message": (
                    f"Communal ID {normalized} zones have no drawable geometry "
                    "to build a primary zone."
                ),
            },
        )

    source_ids = [int(z.id) for z in sources]
    if len(sources) == 1:
        name = str(getattr(sources[0], "name", "") or "").strip() or f"Primary ({normalized})"
    else:
        name = f"Primary ({normalized})"

    geo_ewkt = None
    geometry: dict[str, Any] = {}
    if multipoly is not None:
        geometry["geo_fence_polygon"] = multipoly
        # PostGIS stores EWKT; SQLite test DBs skip the geometry column.
        try:
            bind = db.get_bind()
            dialect = getattr(bind, "dialect", None)
            dialect_name = getattr(dialect, "name", "") if dialect else ""
        except Exception:
            dialect_name = ""
        if dialect_name != "sqlite":
            try:
                geo_ewkt = f"SRID=4326;{geojson_to_wkt(multipoly)}"
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "error_code": "COMMUNAL_ID_GEOMETRY_INVALID",
                        "message": f"Could not build primary geometry: {exc}",
                    },
                ) from exc
        else:
            # Keep GeoJSON in parameters only (no SpatiaLite in unit tests).
            geo_ewkt = None

    config: dict[str, Any] = {
        "h3_cells": h3_cells,
        "h3Cells": h3_cells,
        "sourced_from_communal_id": normalized,
        "source_zone_ids": source_ids,
        "is_communal_primary": True,
        "is_public": False,
    }

    db_zone = Zone(
        zone_id=str(getattr(owner, "zone_id", "") or normalized),
        owner_id=int(owner.id),
        creator_id=int(owner.id),
        zone_type=ZoneType.GEOFENCE,
        name=name[:255],
        description=f"Calculated primary from Communal ID {normalized}",
        is_primary=True,
        active=True,
        h3_cells=h3_cells,
        parameters={
            "contractType": "geofence",
            "geometry": geometry,
            "config": config,
        },
    )
    if geo_ewkt is not None:
        db_zone.geo_fence_polygon = geo_ewkt
        db.add(db_zone)
        db.flush()
        return db_zone

    # SQLite / no SpatiaLite: insert without the Geometry column.
    import json
    from datetime import datetime
    from types import SimpleNamespace

    from sqlalchemy import text

    now = datetime.utcnow().isoformat()
    result = db.execute(
        text(
            "INSERT INTO zones (zone_id, owner_id, creator_id, zone_type, name, "
            "description, h3_cells, parameters, active, is_primary, created_at, updated_at) "
            "VALUES (:zone_id, :owner_id, :creator_id, :zone_type, :name, :description, "
            ":h3_cells, :parameters, 1, 1, :created_at, :updated_at)"
        ),
        {
            "zone_id": str(getattr(owner, "zone_id", "") or normalized),
            "owner_id": int(owner.id),
            "creator_id": int(owner.id),
            "zone_type": "GEOFENCE",
            "name": name[:255],
            "description": f"Calculated primary from Communal ID {normalized}",
            "h3_cells": json.dumps(h3_cells),
            "parameters": json.dumps(
                {
                    "contractType": "geofence",
                    "geometry": geometry,
                    "config": config,
                }
            ),
            "created_at": now,
            "updated_at": now,
        },
    )
    db.flush()
    return SimpleNamespace(
        id=int(result.lastrowid),
        zone_id=str(getattr(owner, "zone_id", "") or normalized),
        owner_id=int(owner.id),
        creator_id=int(owner.id),
        zone_type=ZoneType.GEOFENCE,
        name=name[:255],
        is_primary=True,
        h3_cells=h3_cells,
        parameters={
            "contractType": "geofence",
            "geometry": geometry,
            "config": config,
        },
    )


def apply_individual_communal_subscription(
    db: Session,
    owner,
    raw_communal_id: str | None,
    *,
    required: bool,
) -> str | None:
    """Validate, store Communal ID on owner, and provision the calculated primary.

    Returns the normalized Communal ID when applied, else None.
    """
    account = getattr(owner, "account_type", None)
    account_key = (
        account.value if hasattr(account, "value") else str(account or "")
    ).strip().lower()
    if account_key != AccountType.EXCLUSIVE.value:
        return None

    # Invited members (under another account) do not pick a Communal ID at join.
    root_id = getattr(owner, "account_owner_id", None)
    if root_id is not None and int(root_id) != int(owner.id):
        return None

    raw = (raw_communal_id or "").strip()
    if not raw:
        if required:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "COMMUNAL_ID_REQUIRED",
                    "message": "Individual accounts must select a Communal ID.",
                },
            )
        return None

    normalized = assert_exclusive_communal_id_selectable(db, raw)
    owner.communal_id = normalized
    db.flush()
    provision_individual_primary_from_communal(db, owner, normalized)
    return normalized
