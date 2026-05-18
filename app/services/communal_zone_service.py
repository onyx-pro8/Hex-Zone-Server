"""Resolve communal (and shared reference) zone geometry for validation previews."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.zone import Zone
from app.services.area_boundary_service import lookup_area_boundary

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "communal_catalog.json"
_REFERENCE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")
_DEFAULT_CENTER = (-6.2088, 106.8456)


@dataclass(frozen=True)
class ReferenceZoneResolution:
    reference_id: str
    display_name: str
    geometry: dict[str, Any]
    config: dict[str, Any]
    h3_cells: list[str]
    source: str


def normalize_reference_id(raw: str) -> str:
    return str(raw or "").strip().upper()


def is_valid_reference_format(reference_id: str) -> bool:
    normalized = normalize_reference_id(reference_id)
    return bool(normalized) and bool(_REFERENCE_PATTERN.match(normalized))


def _load_catalog() -> dict[str, Any]:
    if not _CATALOG_PATH.is_file():
        return {}
    try:
        payload = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _catalog_entry(reference_id: str) -> Optional[ReferenceZoneResolution]:
    catalog = _load_catalog()
    row = catalog.get(reference_id)
    if not isinstance(row, dict):
        return None
    polygon = row.get("geo_fence_polygon")
    if not isinstance(polygon, dict):
        return None
    h3_cells = row.get("h3_cells")
    cells = [c for c in h3_cells if isinstance(c, str)] if isinstance(h3_cells, list) else []
    display_name = row.get("display_name")
    name = display_name if isinstance(display_name, str) and display_name.strip() else reference_id
    geometry = {"geo_fence_polygon": polygon}
    config = {"communal_id": reference_id, "h3_cells": cells}
    return ReferenceZoneResolution(
        reference_id=reference_id,
        display_name=name.strip(),
        geometry=geometry,
        config=config,
        h3_cells=cells,
        source="catalog",
    )


def _resolution_from_zone(reference_id: str, zone: Zone) -> Optional[ReferenceZoneResolution]:
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    polygon = geometry.get("geo_fence_polygon")
    if not isinstance(polygon, dict):
        polygon = zone.geo_fence_polygon if isinstance(zone.geo_fence_polygon, dict) else None
    if not isinstance(polygon, dict):
        return None
    h3_cells = config.get("h3_cells") or config.get("h3Cells") or zone.h3_cells or []
    cells = [c for c in h3_cells if isinstance(c, str)] if isinstance(h3_cells, list) else []
    display_name = zone.name if isinstance(zone.name, str) and zone.name.strip() else reference_id
    merged_geometry = dict(geometry)
    merged_geometry["geo_fence_polygon"] = polygon
    merged_config = dict(config)
    merged_config["communal_id"] = reference_id
    if cells:
        merged_config["h3_cells"] = cells
    return ReferenceZoneResolution(
        reference_id=reference_id,
        display_name=display_name.strip(),
        geometry=merged_geometry,
        config=merged_config,
        h3_cells=cells,
        source="existing_zone",
    )


def _find_existing_zone_resolution(
    db: Session,
    owner_ids: list[int],
    reference_id: str,
) -> Optional[ReferenceZoneResolution]:
    if not owner_ids:
        return None
    zones = (
        db.query(Zone)
        .filter(Zone.owner_id.in_(owner_ids), Zone.active.is_(True))
        .all()
    )
    for zone in zones:
        params = zone.parameters if isinstance(zone.parameters, dict) else {}
        config = params.get("config") if isinstance(params.get("config"), dict) else {}
        stored = config.get("communal_id") or config.get("communalId")
        if normalize_reference_id(str(stored or "")) != reference_id:
            continue
        resolved = _resolution_from_zone(reference_id, zone)
        if resolved:
            return resolved
    return None


def _deterministic_resolution(reference_id: str) -> ReferenceZoneResolution:
    digest = hashlib.sha256(reference_id.encode("utf-8")).digest()
    lat_offset = ((digest[0] / 255) - 0.5) * 0.08
    lng_offset = ((digest[1] / 255) - 0.5) * 0.08
    base_lat, base_lng = _DEFAULT_CENTER
    center_lat = base_lat + lat_offset
    center_lng = base_lng + lng_offset
    half = 0.0035
    ring = [
        [center_lng - half, center_lat - half],
        [center_lng + half, center_lat - half],
        [center_lng + half, center_lat + half],
        [center_lng - half, center_lat + half],
        [center_lng - half, center_lat - half],
    ]
    polygon = {"type": "Polygon", "coordinates": [ring]}
    geometry = {"geo_fence_polygon": polygon}
    config = {"communal_id": reference_id, "h3_cells": []}
    return ReferenceZoneResolution(
        reference_id=reference_id,
        display_name=f"Community {reference_id}",
        geometry=geometry,
        config=config,
        h3_cells=[],
        source="generated_geometry",
    )


def _resolution_from_osm_boundary(reference_id: str) -> Optional[ReferenceZoneResolution]:
    boundary = lookup_area_boundary(reference_id)
    if not boundary:
        return None
    polygon, display_name = boundary
    geometry = {"geo_fence_polygon": polygon}
    config = {"communal_id": reference_id, "h3_cells": []}
    return ReferenceZoneResolution(
        reference_id=reference_id,
        display_name=display_name,
        geometry=geometry,
        config=config,
        h3_cells=[],
        source="osm_boundary",
    )


def resolve_communal_reference(
    db: Session,
    owner_ids: list[int],
    reference_id: str,
) -> Optional[ReferenceZoneResolution]:
    normalized = normalize_reference_id(reference_id)
    if not is_valid_reference_format(normalized):
        return None
    from_zone = _find_existing_zone_resolution(db, owner_ids, normalized)
    if from_zone:
        return from_zone
    from_osm = _resolution_from_osm_boundary(normalized)
    if from_osm:
        return from_osm
    from_catalog = _catalog_entry(normalized)
    if from_catalog:
        return from_catalog
    return _deterministic_resolution(normalized)


def generate_communal_reference(
    db: Session,
    owner_ids: list[int],
) -> ReferenceZoneResolution:
    catalog = _load_catalog()
    for _ in range(24):
        candidate = f"COMM-{secrets.token_hex(3).upper()}"
        if candidate in catalog:
            continue
        if _find_existing_zone_resolution(db, owner_ids, candidate):
            continue
        return _deterministic_resolution(candidate)
    fallback = f"COMM-{secrets.token_hex(4).upper()}"
    return _deterministic_resolution(fallback)


def resolution_to_response_payload(resolution: ReferenceZoneResolution) -> dict[str, Any]:
    return {
        "valid": True,
        "zone_type": "communal_id",
        "reference_id": resolution.reference_id,
        "display_name": resolution.display_name,
        "geometry": resolution.geometry,
        "config": resolution.config,
        "h3_cells": resolution.h3_cells,
        "source": resolution.source,
    }
