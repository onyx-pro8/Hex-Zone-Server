"""Zone services with contract type mappings and constraints."""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Owner, Zone
from app.models.zone import ZoneType
from app.core.h3_utils import has_h3_overlap, validate_h3_cell
from app.services.access_policy import visible_zone_owner_ids
from app.services.zone_policy import (
    account_owner_ids_for_policy,
    build_capabilities,
    count_zones_for_owners,
    enforce_can_create,
    ensure_unique_zone_name,
    ensure_zone_edit_allowed,
    normalize_zone_name,
)

CONTRACT_TO_MODEL_ZONE_TYPE = {
    "polygon": ZoneType.GEOFENCE,
    "geofence": ZoneType.GEOFENCE,
    "circle": ZoneType.WARN,
    "warn": ZoneType.WARN,
    "grid": ZoneType.ALERT,
    "alert": ZoneType.ALERT,
    "dynamic": ZoneType.EMERGENCY,
    "emergency": ZoneType.EMERGENCY,
    "communal_id": ZoneType.CUSTOM_1,
    "custom_1": ZoneType.CUSTOM_1,
    "government_local_code": ZoneType.CUSTOM_2,
    "proximity": ZoneType.RESTRICTED,
    "restricted": ZoneType.RESTRICTED,
    "object": ZoneType.CUSTOM_2,
    "custom_2": ZoneType.CUSTOM_2,
}

MODEL_TO_CONTRACT_ZONE_TYPE = {
    ZoneType.GEOFENCE: "geofence",
    ZoneType.WARN: "warn",
    ZoneType.ALERT: "grid",
    ZoneType.EMERGENCY: "dynamic",
    ZoneType.RESTRICTED: "proximity",
    ZoneType.CUSTOM_1: "communal_id",
    ZoneType.CUSTOM_2: "government_local_code",
}


def _extract_geojson_polygon(geometry: object) -> dict | None:
    """Return GeoJSON Polygon/MultiPolygon dict, otherwise None.

    Dashboard clients send ``geometry: { geo_fence_polygon: { type, coordinates } }``
    while some legacy callers pass a top-level Polygon/MultiPolygon object.
    """
    if not isinstance(geometry, dict):
        return None
    geometry_type = geometry.get("type")
    if geometry_type in {"Polygon", "MultiPolygon"}:
        return geometry
    nested = geometry.get("geo_fence_polygon")
    if isinstance(nested, dict) and nested.get("type") in {"Polygon", "MultiPolygon"}:
        return nested
    return None


def _serialize_zone(zone: Zone) -> dict:
    contract_type = (zone.parameters or {}).get("contractType")
    config = dict((zone.parameters or {}).get("config", {}) or {})
    # Always source h3 cells from canonical DB column.
    config["h3Cells"] = list(zone.h3_cells or [])
    return {
        "id": zone.id,
        "zone_id": zone.zone_id,
        "owner_id": zone.owner_id,
        "creator_id": zone.creator_id,
        "name": zone.name,
        "type": contract_type or MODEL_TO_CONTRACT_ZONE_TYPE.get(zone.zone_type, "dynamic"),
        "geometry": (zone.parameters or {}).get("geometry", {}),
        "config": config,
    }


def create_zone(db: Session, owner: Owner, payload: dict) -> dict:
    account_owner_ids = account_owner_ids_for_policy(db, owner)
    total_zones = count_zones_for_owners(db, account_owner_ids)
    capabilities = build_capabilities(owner.role.value, total_zones)
    enforce_can_create(capabilities)

    zone_type = payload["type"]
    if zone_type not in CONTRACT_TO_MODEL_ZONE_TYPE:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported zone type")
    geometry = payload.get("geometry", {})
    geo_fence_polygon = _extract_geojson_polygon(geometry)
    config = payload.get("config", {}) or {}
    h3_cells = config.get("h3Cells", []) if isinstance(config, dict) else []
    if h3_cells:
        if any(not validate_h3_cell(cell) for cell in h3_cells):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid H3 cell id")
        if has_h3_overlap(h3_cells):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Overlapping H3 cells are not allowed across resolutions",
            )

    normalized_name = normalize_zone_name(payload.get("name"))
    ensure_unique_zone_name(db, account_owner_ids, normalized_name)

    zone = Zone(
        zone_id=payload.get("id") or owner.zone_id,
        owner_id=owner.id,
        creator_id=owner.id,
        zone_type=CONTRACT_TO_MODEL_ZONE_TYPE[zone_type],
        name=normalized_name,
        parameters={
            "contractType": zone_type,
            "geometry": geometry,
            "config": config,
        },
        h3_cells=h3_cells,
        geo_fence_polygon=geo_fence_polygon,
    )
    db.add(zone)
    db.flush()
    db.refresh(zone)
    return _serialize_zone(zone)


def list_zones(db: Session, owner: Owner) -> list[dict]:
    owner_ids = visible_zone_owner_ids(db, owner)
    zones = (
        db.query(Zone)
        .filter(Zone.owner_id.in_(owner_ids), Zone.active.is_(True))
        .all()
    )
    return [_serialize_zone(zone) for zone in zones]


def update_zone(db: Session, owner: Owner, zone_id: str, payload: dict) -> dict:
    zone = db.query(Zone).filter(Zone.owner_id == owner.id, Zone.zone_id == zone_id).first()
    if not zone:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Zone not found")
    ensure_zone_edit_allowed(owner, zone)
    if payload.get("name") is not None:
        normalized_name = normalize_zone_name(payload["name"])
        owner_ids = account_owner_ids_for_policy(db, owner)
        ensure_unique_zone_name(db, owner_ids, normalized_name, exclude_zone_record_id=zone.id)
        zone.name = normalized_name
    if payload.get("type"):
        zone_type = payload["type"]
        if zone_type not in CONTRACT_TO_MODEL_ZONE_TYPE:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported zone type")
        zone.zone_type = CONTRACT_TO_MODEL_ZONE_TYPE[zone_type]
    params = zone.parameters or {}
    if "geometry" in payload:
        geometry = payload.get("geometry", {})
        params["geometry"] = geometry
        zone.geo_fence_polygon = _extract_geojson_polygon(geometry)
    if "config" in payload:
        config = payload.get("config", {}) or {}
        h3_cells = config.get("h3Cells", []) if isinstance(config, dict) else []
        if h3_cells:
            if any(not validate_h3_cell(cell) for cell in h3_cells):
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid H3 cell id")
            if has_h3_overlap(h3_cells):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Overlapping H3 cells are not allowed across resolutions",
                )
        params["config"] = config
        zone.h3_cells = h3_cells
    if payload.get("type"):
        params["contractType"] = payload["type"]
    zone.parameters = params
    db.flush()
    return _serialize_zone(zone)


def delete_zone(db: Session, owner: Owner, zone_id: str) -> None:
    zone = db.query(Zone).filter(Zone.owner_id == owner.id, Zone.zone_id == zone_id).first()
    if not zone:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Zone not found")
    db.delete(zone)
