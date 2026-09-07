"""Zone services with contract type mappings and constraints."""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Owner, Zone
from app.models.zone import ZoneType
from app.core.h3_utils import has_h3_overlap, validate_h3_cell
from app.services.access_policy import visible_zone_owner_ids
from app.services.zone_policy import (
    account_owner_ids_for_policy,
    ensure_unique_zone_name,
    ensure_zone_delete_allowed,
    ensure_zone_edit_allowed,
    list_zones_visibility_filter,
    normalize_zone_name,
    prepare_zone_tier_on_create,
    zone_is_primary,
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


def _owner_display_name(owner: Owner | None) -> str | None:
    if owner is None:
        return None
    label = (owner.message_display_name or "").strip()
    return label or None


def _serialize_zone(
    zone: Zone,
    *,
    owners_by_id: dict[int, Owner] | None = None,
    evicted_zones: list[dict] | None = None,
) -> dict:
    contract_type = (zone.parameters or {}).get("contractType")
    config = dict((zone.parameters or {}).get("config", {}) or {})
    # Always source h3 cells from canonical DB column.
    config["h3Cells"] = list(zone.h3_cells or [])
    preferred_id = (
        int(zone.creator_id) if zone.creator_id is not None else int(zone.owner_id)
    )
    owner_name = None
    if owners_by_id is not None:
        owner_name = _owner_display_name(owners_by_id.get(preferred_id))
    payload = {
        "id": zone.id,
        "zone_id": zone.zone_id,
        "owner_id": zone.owner_id,
        "creator_id": zone.creator_id,
        "owner_name": owner_name,
        "name": zone.name,
        "type": contract_type or MODEL_TO_CONTRACT_ZONE_TYPE.get(zone.zone_type, "dynamic"),
        "geometry": (zone.parameters or {}).get("geometry", {}),
        "config": config,
        "is_primary": zone_is_primary(zone),
    }
    if evicted_zones:
        payload["evicted_zones"] = evicted_zones
    return payload


def create_zone(db: Session, owner: Owner, payload: dict) -> dict:
    is_primary, evicted = prepare_zone_tier_on_create(db, owner)

    account_owner_ids = account_owner_ids_for_policy(db, owner)

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
        is_primary=is_primary,
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
    return _serialize_zone(
        zone,
        owners_by_id={int(owner.id): owner},
        evicted_zones=[item.to_dict() for item in evicted] or None,
    )


def list_zones(db: Session, owner: Owner) -> list[dict]:
    owner_ids = visible_zone_owner_ids(db, owner)
    query = db.query(Zone).filter(Zone.owner_id.in_(owner_ids), Zone.active.is_(True))
    visibility = list_zones_visibility_filter(owner)
    if visibility is not None:
        query = query.filter(visibility)
    zones = query.all()
    lookup_ids: set[int] = set()
    for zone in zones:
        lookup_ids.add(int(zone.owner_id))
        if zone.creator_id is not None:
            lookup_ids.add(int(zone.creator_id))
    owners: dict[int, Owner] = {}
    if lookup_ids:
        rows = db.query(Owner).filter(Owner.id.in_(tuple(lookup_ids))).all()
        owners = {int(row.id): row for row in rows}
    return [_serialize_zone(zone, owners_by_id=owners) for zone in zones]


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
    preferred_id = (
        int(zone.creator_id) if zone.creator_id is not None else int(zone.owner_id)
    )
    named = db.query(Owner).filter(Owner.id == preferred_id).first()
    owners = {preferred_id: named} if named is not None else {}
    return _serialize_zone(zone, owners_by_id=owners)


def delete_zone(db: Session, owner: Owner, zone_id: str) -> None:
    zone = db.query(Zone).filter(Zone.zone_id == zone_id).first()
    if not zone:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Zone not found")
    ensure_zone_delete_allowed(db, owner, zone)
    db.delete(zone)


async def notify_zone_evictions(db: Session, *, admin: Owner, evicted: list) -> None:
    """Notify admin + affected members when secondary zones are auto-removed."""
    if not evicted:
        return
    from app.services import push_notification_service
    from app.websocket.manager import ws_manager

    by_creator: dict[int, list] = {}
    for item in evicted:
        creator_id = int(item["creator_id"] if isinstance(item, dict) else item.creator_id)
        by_creator.setdefault(creator_id, []).append(item)

    def _as_dict(item) -> dict:
        return item if isinstance(item, dict) else item.to_dict()

    def _name(item) -> str:
        return str(item["name"] if isinstance(item, dict) else item.name)

    admin_payload = {
        "message": (
            f"{len(evicted)} member secondary zone(s) were removed automatically "
            "to make room for an additional primary zone."
        ),
        "evicted_zones": [_as_dict(item) for item in evicted],
        "reason": "primary_quota_rebalance",
    }
    try:
        await ws_manager.broadcast_to_users([admin.id], "ZONE_EVICTED", admin_payload)
    except Exception:
        pass

    for creator_id, items in by_creator.items():
        names = ", ".join(f'"{_name(z)}"' for z in items)
        member_payload = {
            "message": (
                f"Your secondary zone {names} was removed automatically because "
                "the administrator created another primary zone."
            ),
            "evicted_zones": [_as_dict(z) for z in items],
            "reason": "primary_quota_rebalance",
        }
        try:
            await ws_manager.broadcast_to_users(
                [creator_id], "ZONE_EVICTED", member_payload
            )
        except Exception:
            pass
        try:
            await push_notification_service.send_plain_push_to_owners(
                db,
                [creator_id],
                title="Zone removed",
                body=member_payload["message"],
                data={"event": "ZONE_EVICTED", "reason": "primary_quota_rebalance"},
            )
        except Exception:
            pass
