"""Resolve government local area codes (postal, district, admin) to zone polygons."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.zone import Zone
from app.services.area_boundary_service import (
    AreaLocationInput,
    lookup_area_boundary,
    lookup_global_area_boundary,
    resolve_country_iso2,
    parse_area_location_from_dict,
)
from app.services.communal_zone_service import ReferenceZoneResolution

_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "government_local_catalog.json"
)
_LOCAL_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._|-]{2,127}$")
_DEFAULT_CENTER = (-6.2088, 106.8456)


def normalize_local_area_code(raw: str) -> str:
    """Normalize postal / district / admin codes for lookup."""
    compact = re.sub(r"\s+", "", str(raw or "").strip())
    return compact.upper()


def is_valid_local_code_format(local_code: str) -> bool:
    normalized = normalize_local_area_code(local_code)
    return bool(normalized) and bool(_LOCAL_CODE_PATTERN.match(normalized))


def _resolution_from_location(location: AreaLocationInput) -> Optional[ReferenceZoneResolution]:
    boundary = lookup_global_area_boundary(location)
    if not boundary:
        return None
    polygon, display_name, config = boundary
    geometry = {"geo_fence_polygon": polygon}
    return ReferenceZoneResolution(
        reference_id=location.reference_id(),
        display_name=display_name,
        geometry=geometry,
        config=config,
        h3_cells=[],
        source="osm_boundary",
    )


def resolve_government_address(
    db: Session,
    owner_ids: list[int],
    address: dict[str, Any],
) -> Optional[ReferenceZoneResolution]:
    location = parse_area_location_from_dict(address)
    if not location:
        return None
    ref_id = location.reference_id()
    from_zone = _find_existing_zone_resolution(db, owner_ids, ref_id)
    if from_zone:
        return from_zone
    from_osm = _resolution_from_location(location)
    if from_osm:
        return from_osm
    return None


def _load_catalog() -> dict[str, Any]:
    if not _CATALOG_PATH.is_file():
        return {}
    try:
        payload = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _catalog_keys_for_lookup(local_code: str) -> list[str]:
    """Try exact key and common postal variants."""
    keys = [local_code]
    if local_code.isdigit() and len(local_code) == 5:
        keys.append(f"POSTAL-{local_code}")
    return keys


def _catalog_entry(local_code: str) -> Optional[ReferenceZoneResolution]:
    catalog = _load_catalog()
    row: dict[str, Any] | None = None
    for key in _catalog_keys_for_lookup(local_code):
        candidate = catalog.get(key)
        if isinstance(candidate, dict):
            row = candidate
            break
    if not row:
        return None
    polygon = row.get("geo_fence_polygon")
    if not isinstance(polygon, dict):
        return None
    h3_cells = row.get("h3_cells")
    cells = [c for c in h3_cells if isinstance(c, str)] if isinstance(h3_cells, list) else []
    display_name = row.get("display_name")
    name = display_name if isinstance(display_name, str) and display_name.strip() else local_code
    code_type = row.get("code_type")
    geometry = {"geo_fence_polygon": polygon}
    config: dict[str, Any] = {
        "local_code": local_code,
        "area_code": local_code,
        "h3_cells": cells,
    }
    if isinstance(code_type, str) and code_type.strip():
        config["code_type"] = code_type.strip()
    return ReferenceZoneResolution(
        reference_id=local_code,
        display_name=name.strip(),
        geometry=geometry,
        config=config,
        h3_cells=cells,
        source="catalog",
    )


def _resolution_from_zone(local_code: str, zone: Zone) -> Optional[ReferenceZoneResolution]:
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
    display_name = zone.name if isinstance(zone.name, str) and zone.name.strip() else local_code
    merged_geometry = dict(geometry)
    merged_geometry["geo_fence_polygon"] = polygon
    merged_config = dict(config)
    merged_config["local_code"] = local_code
    merged_config["area_code"] = local_code
    if cells:
        merged_config["h3_cells"] = cells
    return ReferenceZoneResolution(
        reference_id=local_code,
        display_name=display_name.strip(),
        geometry=merged_geometry,
        config=merged_config,
        h3_cells=cells,
        source="existing_zone",
    )


def _find_existing_zone_resolution(
    db: Session,
    owner_ids: list[int],
    local_code: str,
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
        stored = (
            config.get("local_code")
            or config.get("localCode")
            or config.get("area_code")
            or config.get("areaCode")
        )
        if normalize_local_area_code(str(stored or "")) != local_code:
            continue
        resolved = _resolution_from_zone(local_code, zone)
        if resolved:
            return resolved
    return None


def _resolution_from_osm_boundary(
    local_code: str,
    *,
    country_iso2: str | None = None,
) -> Optional[ReferenceZoneResolution]:
    catalog = _load_catalog()
    code_type = None
    row = catalog.get(local_code)
    if isinstance(row, dict) and isinstance(row.get("code_type"), str):
        code_type = row.get("code_type")
    boundary = lookup_area_boundary(
        local_code,
        code_type=code_type,
        country_iso2=country_iso2,
    )
    if not boundary:
        return None
    polygon, display_name = boundary
    geometry = {"geo_fence_polygon": polygon}
    config: dict[str, Any] = {
        "local_code": local_code,
        "area_code": local_code,
        "h3_cells": [],
    }
    if code_type:
        config["code_type"] = code_type
    elif local_code.isdigit():
        config["code_type"] = "postal"
    else:
        config["code_type"] = "district"
    return ReferenceZoneResolution(
        reference_id=local_code,
        display_name=display_name,
        geometry=geometry,
        config=config,
        h3_cells=[],
        source="osm_boundary",
    )


def _deterministic_resolution(local_code: str) -> ReferenceZoneResolution:
    digest = hashlib.sha256(local_code.encode("utf-8")).digest()
    lat_offset = ((digest[0] / 255) - 0.5) * 0.06
    lng_offset = ((digest[1] / 255) - 0.5) * 0.06
    base_lat, base_lng = _DEFAULT_CENTER
    center_lat = base_lat + lat_offset
    center_lng = base_lng + lng_offset
    half = 0.0045 if local_code.isdigit() else 0.008
    ring = [
        [center_lng - half, center_lat - half],
        [center_lng + half, center_lat - half],
        [center_lng + half, center_lat + half],
        [center_lng - half, center_lat + half],
        [center_lng - half, center_lat - half],
    ]
    polygon = {"type": "Polygon", "coordinates": [ring]}
    code_type = "postal" if local_code.isdigit() else "district"
    geometry = {"geo_fence_polygon": polygon}
    config = {
        "local_code": local_code,
        "area_code": local_code,
        "code_type": code_type,
        "h3_cells": [],
    }
    return ReferenceZoneResolution(
        reference_id=local_code,
        display_name=f"Area {local_code}",
        geometry=geometry,
        config=config,
        h3_cells=[],
        source="generated_geometry",
    )


def resolve_government_local_code(
    db: Session,
    owner_ids: list[int],
    local_code: str,
    *,
    address: dict[str, Any] | None = None,
) -> Optional[ReferenceZoneResolution]:
    if address and str(address.get("country") or "").strip():
        resolved = resolve_government_address(db, owner_ids, address)
        if resolved:
            return resolved

    normalized = normalize_local_area_code(local_code)
    if not is_valid_local_code_format(normalized):
        return None
    from_zone = _find_existing_zone_resolution(db, owner_ids, normalized)
    if from_zone:
        return from_zone
    iso2 = None
    if address:
        iso2 = resolve_country_iso2(str(address.get("country") or ""))
    from_osm = _resolution_from_osm_boundary(normalized, country_iso2=iso2)
    if from_osm:
        return from_osm
    from_catalog = _catalog_entry(normalized)
    if from_catalog:
        return from_catalog
    return None


def resolution_to_response_payload(resolution: ReferenceZoneResolution) -> dict[str, Any]:
    return {
        "valid": True,
        "zone_type": "government_local_code",
        "reference_id": resolution.reference_id,
        "display_name": resolution.display_name,
        "geometry": resolution.geometry,
        "config": resolution.config,
        "h3_cells": resolution.h3_cells,
        "source": resolution.source,
    }
