"""Universal access codes: request, approve, enable, and zone visibility."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, defer

from app.domain.message_types import CanonicalMessageType, type_category, type_scope
from app.models.owner import Owner, OwnerRole
from app.models.universal_access import (
    UniversalAccessCredential,
    UniversalAccessRequest,
    UniversalAccessStatus,
)
from app.models.zone import Zone
from app.models.zone_message_event import ZoneMessageEvent
from app.services.access_policy import messaging_visible_owner_ids
from app.services.account_type_policy import is_system_administrator

logger = logging.getLogger(__name__)

_MAX_PHOTO_CHARS = 1_500_000
_MAX_UNIVERSAL_ZONES = 200


def _clean(value: str, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _require_network_admin(owner: Owner) -> None:
    if is_system_administrator(owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System administrators review universal access requests. They do not file them.",
        )
    if (owner.role.value or "").strip().lower() != "administrator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a network administrator can request a universal access code.",
        )


def _place_label(country: str, region: str, city: str) -> str:
    return ", ".join(part for part in (city, region, country) if part)


def _owner_name(owner: Owner | None) -> str:
    if owner is None:
        return ""
    full = f"{owner.first_name} {owner.last_name}".strip()
    return full or owner.email


def _bbox_from_polygon(polygon: dict[str, Any] | None) -> list[float] | None:
    if not isinstance(polygon, dict):
        return None
    points: list[list[float]] = []

    def _walk(node: object) -> None:
        if isinstance(node, list):
            if len(node) >= 2 and isinstance(node[0], (int, float)) and isinstance(node[1], (int, float)):
                points.append([float(node[0]), float(node[1])])
                return
            for item in node:
                _walk(item)

    _walk(polygon.get("coordinates"))
    if not points:
        return None
    lngs = [p[0] for p in points]
    lats = [p[1] for p in points]
    return [min(lngs), min(lats), max(lngs), max(lats)]


def _resolve_place_bbox(country: str, region: str, city: str) -> list[float] | None:
    """Best-effort city box. Approval still succeeds when lookup is unavailable."""
    try:
        from app.services.area_boundary_service import (
            AreaLocationInput,
            lookup_global_area_boundary,
        )

        location = AreaLocationInput(country=country, city=f"{city}, {region}".strip(", "))
        found = lookup_global_area_boundary(location)
    except Exception:
        return None
    if not found:
        return None
    polygon, _name, _config = found
    return _bbox_from_polygon(polygon if isinstance(polygon, dict) else None)


def _new_code(db: Session) -> str:
    for _ in range(8):
        raw = secrets.token_hex(4).upper()
        code = f"UAC-{raw[:4]}-{raw[4:]}"
        exists = (
            db.query(UniversalAccessCredential.id)
            .filter(UniversalAccessCredential.code == code)
            .first()
        )
        if exists is None:
            return code
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not generate a unique access code.",
    )


def _request_row(
    db: Session,
    row: UniversalAccessRequest,
    *,
    include_photo: bool,
) -> dict[str, Any]:
    admin = db.get(Owner, row.network_admin_id)
    credential = (
        db.query(UniversalAccessCredential)
        .filter(UniversalAccessCredential.request_id == row.id)
        .first()
    )
    payload: dict[str, Any] = {
        "id": row.id,
        "member_id": row.member_id,
        "member_name": row.member_name,
        "email": row.email,
        "address": row.address,
        "country": row.country,
        "region": row.region,
        "city": row.city,
        "place": _place_label(row.country, row.region, row.city),
        "note": row.note or "",
        "status": row.status.value if hasattr(row.status, "value") else str(row.status),
        "reject_reason": row.reject_reason or "",
        "network_id": admin.zone_id if admin else "",
        "network_admin_name": _owner_name(admin),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "code": credential.code if credential else None,
        "credential_id": credential.id if credential else None,
        "enabled": credential.enabled if credential else None,
    }
    if include_photo:
        payload["photo_id"] = row.photo_id
    return payload


def create_request(
    db: Session,
    *,
    admin: Owner,
    member_id: int,
    address: str,
    photo_id: str,
    country: str,
    region: str,
    city: str,
    note: str = "",
) -> dict[str, Any]:
    _require_network_admin(admin)
    photo = str(photo_id or "").strip()
    if not photo.startswith("data:image/"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Photo ID must be an image.",
        )
    if len(photo) > _MAX_PHOTO_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Photo ID is too large.",
        )
    country_v = _clean(country, limit=120)
    region_v = _clean(region, limit=120)
    city_v = _clean(city, limit=120)
    address_v = _clean(address, limit=255)
    if not country_v or not region_v or not city_v or not address_v:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Address, country, region, and city are required.",
        )

    visible = set(messaging_visible_owner_ids(db, admin, include_inactive=False))
    if int(member_id) not in visible:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That member is not on your network.",
        )
    member = db.get(Owner, int(member_id))
    if member is None or not member.active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That member is not on your network.",
        )

    row = UniversalAccessRequest(
        network_admin_id=admin.id,
        member_id=member.id,
        member_name=_owner_name(member),
        email=member.email,
        address=address_v,
        photo_id=photo,
        country=country_v,
        region=region_v,
        city=city_v,
        note=_clean(note, limit=1000),
        status=UniversalAccessStatus.PENDING,
    )
    db.add(row)
    db.flush()
    notice = _record_request_notice(db, row, admin)
    db.commit()
    db.refresh(row)
    payload = _request_row(db, row, include_photo=True)
    if notice is not None:
        payload["notice_event_id"] = notice.id
    return payload


def _system_admin_ids(db: Session) -> list[int]:
    rows = (
        db.query(Owner)
        .filter(Owner.role == OwnerRole.ADMINISTRATOR, Owner.active.is_(True))
        .all()
    )
    return [row.id for row in rows if is_system_administrator(row)]


def _record_request_notice(
    db: Session,
    row: UniversalAccessRequest,
    admin: Owner,
) -> ZoneMessageEvent | None:
    """Persist a PERMISSION history row for system administrators.

    The photo stays on the request. The inbox line is the same kind of access
    history a guest permission leaves behind.
    """
    targets = _system_admin_ids(db)
    if not targets:
        return None
    place = _place_label(row.country, row.region, row.city)
    text = f"Universal access code requested for {row.member_name} ({place})."
    event = ZoneMessageEvent(
        zone_id=(admin.zone_id or "").strip() or "universal-access",
        sender_id=admin.id,
        receiver_id=targets[0],
        type=CanonicalMessageType.PERMISSION.value,
        category=type_category(CanonicalMessageType.PERMISSION),
        scope=type_scope(CanonicalMessageType.PERMISSION),
        text=text,
        body_json={
            "code": "UNIVERSAL_ACCESS_REQUESTED",
            "universal_access_request_id": row.id,
            "member_id": row.member_id,
            "member_name": row.member_name,
            "place": place,
            "note": row.note or "",
        },
        metadata_json={
            "flow": "universal_access_request",
            "permission_visibility": "direct",
            "universal_access_request_id": row.id,
            "delivered_owner_ids": targets,
            "sender_message": {"code": "UNIVERSAL_ACCESS_REQUESTED", "text": text},
            "member_message": {"code": "UNIVERSAL_ACCESS_REQUESTED", "text": text},
        },
    )
    db.add(event)
    db.flush()
    return event


async def notify_system_admins_of_request(db: Session, event_id: str) -> None:
    """Push, inbox socket, and smart-hub webhook. Failures do not undo the request."""
    event = db.get(ZoneMessageEvent, event_id)
    if event is None:
        return
    meta = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    targets = [
        int(owner_id)
        for owner_id in (meta.get("delivered_owner_ids") or [])
        if isinstance(owner_id, int) and not isinstance(owner_id, bool)
    ]
    if not targets:
        return
    try:
        from app.services import push_notification_service, smart_home_webhook_service
        from app.services.guest_api_service import zone_message_event_to_member_zone_message_response
        from app.websocket.manager import ws_manager

        payload = zone_message_event_to_member_zone_message_response(
            event,
            db=db,
            viewer_owner_id=targets[0],
        ).model_dump(mode="json")
        await ws_manager.broadcast_to_users(targets, "NEW_MESSAGE", payload)
        await push_notification_service.send_plain_push_to_owners(
            db,
            targets,
            title="Universal access request",
            body=(event.text or "A network requested a universal access code.")[:240],
            data={
                "event": "NEW_MESSAGE",
                "type": "PERMISSION",
                "id": str(event.id),
                "zone_id": str(event.zone_id or ""),
            },
        )
        zone = str(event.zone_id or "").strip()
        webhook_payload = {
            "type": CanonicalMessageType.PERMISSION.value,
            "text": event.text,
            "sender_id": event.sender_id,
            "zone_id": zone,
            "delivered_owner_ids": targets,
            "metadata": {
                "sender_network_id": zone,
                "flow": "universal_access_request",
            },
        }
        webhook_stats = await smart_home_webhook_service.send_smart_home_webhooks(
            db,
            targets,
            webhook_payload,
            require_pushable=False,
        )
        await smart_home_webhook_service.notify_smart_home_webhook_owners(
            list(webhook_stats.get("webhook_owner_results") or [])
        )
    except Exception:
        logger.exception("Universal access request notice failed for event %s", event_id)


def list_requests(db: Session, *, caller: Owner) -> list[dict[str, Any]]:
    if (caller.role.value or "").strip().lower() != "administrator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can view universal access requests.",
        )
    query = db.query(UniversalAccessRequest)
    if not is_system_administrator(caller):
        query = query.filter(UniversalAccessRequest.network_admin_id == caller.id)
    rows = query.order_by(UniversalAccessRequest.id.desc()).all()
    include_photo = is_system_administrator(caller)
    return [_request_row(db, row, include_photo=include_photo) for row in rows]


def get_request(db: Session, *, caller: Owner, request_id: int) -> dict[str, Any]:
    row = db.get(UniversalAccessRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    if not is_system_administrator(caller) and int(row.network_admin_id) != int(caller.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request not found.")
    if (caller.role.value or "").strip().lower() != "administrator":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request not found.")
    return _request_row(db, row, include_photo=True)


def approve_request(
    db: Session,
    *,
    caller: Owner,
    request_id: int,
    resolve_boundary: bool = True,
) -> dict[str, Any]:
    if not is_system_administrator(caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the system administrator can approve a universal access code.",
        )
    row = db.get(UniversalAccessRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    if row.status != UniversalAccessStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request was already reviewed.",
        )
    bbox = _resolve_place_bbox(row.country, row.region, row.city) if resolve_boundary else None
    credential = UniversalAccessCredential(
        request_id=row.id,
        member_id=row.member_id,
        network_admin_id=row.network_admin_id,
        code=_new_code(db),
        country=row.country,
        region=row.region,
        city=row.city,
        enabled=True,
        place_bbox=bbox,
    )
    row.status = UniversalAccessStatus.APPROVED
    row.reviewed_by_id = caller.id
    row.reviewed_at = datetime.utcnow()
    row.reject_reason = ""
    db.add(credential)
    db.commit()
    db.refresh(row)
    return _request_row(db, row, include_photo=True)


def reject_request(
    db: Session,
    *,
    caller: Owner,
    request_id: int,
    reason: str,
) -> dict[str, Any]:
    if not is_system_administrator(caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the system administrator can reject a universal access code.",
        )
    row = db.get(UniversalAccessRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    if row.status != UniversalAccessStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request was already reviewed.",
        )
    cleaned = _clean(reason, limit=500)
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A reject reason is required.",
        )
    row.status = UniversalAccessStatus.REJECTED
    row.reject_reason = cleaned
    row.reviewed_by_id = caller.id
    row.reviewed_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _request_row(db, row, include_photo=True)


def set_credential_enabled(
    db: Session,
    *,
    caller: Owner,
    credential_id: int,
    enabled: bool,
) -> dict[str, Any]:
    credential = db.get(UniversalAccessCredential, credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found.")
    allowed = is_system_administrator(caller) or int(credential.network_admin_id) == int(caller.id)
    if not allowed or (caller.role.value or "").strip().lower() != "administrator":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Credential not found.")
    credential.enabled = bool(enabled)
    db.commit()
    request_row = db.get(UniversalAccessRequest, credential.request_id)
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    return _request_row(db, request_row, include_photo=is_system_administrator(caller))


def _config_of(zone: Zone) -> dict[str, Any]:
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    return config


def _centroid(zone: Zone) -> tuple[float, float] | None:
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    polygon = geometry.get("geo_fence_polygon")
    if not isinstance(polygon, dict):
        polygon = zone.geo_fence_polygon if isinstance(zone.geo_fence_polygon, dict) else None
    if not isinstance(polygon, dict):
        return None
    ring: list | None = None
    coords = polygon.get("coordinates")
    if polygon.get("type") == "Polygon" and isinstance(coords, list) and coords:
        ring = coords[0]
    elif polygon.get("type") == "MultiPolygon" and isinstance(coords, list) and coords:
        first = coords[0]
        if isinstance(first, list) and first:
            ring = first[0]
    if not isinstance(ring, list) or len(ring) < 3:
        return None
    lngs: list[float] = []
    lats: list[float] = []
    for point in ring:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            lngs.append(float(point[0]))
            lats.append(float(point[1]))
    if not lngs:
        return None
    return sum(lngs) / len(lngs), sum(lats) / len(lats)


def _text_match(credential: UniversalAccessCredential, zone: Zone) -> bool:
    config = _config_of(zone)
    city = str(config.get("city") or "").strip().lower()
    region = str(config.get("region") or config.get("state") or "").strip().lower()
    country = str(config.get("country") or "").strip().lower()
    if not city and not region and not country:
        return False
    if city and city != credential.city.strip().lower():
        return False
    if region and region != credential.region.strip().lower():
        return False
    if country and country != credential.country.strip().lower():
        return False
    return True


def _bbox_match(credential: UniversalAccessCredential, zone: Zone) -> bool:
    bbox = credential.place_bbox
    if not isinstance(bbox, list) or len(bbox) != 4:
        return False
    centroid = _centroid(zone)
    if centroid is None:
        return False
    lng, lat = centroid
    west, south, east, north = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    return west <= lng <= east and south <= lat <= north


def _zone_in_credential(credential: UniversalAccessCredential, zone: Zone) -> bool:
    if isinstance(credential.place_bbox, list) and len(credential.place_bbox) == 4:
        return _bbox_match(credential, zone)
    return _text_match(credential, zone)


def enabled_credentials_for(db: Session, owner: Owner) -> list[UniversalAccessCredential]:
    return (
        db.query(UniversalAccessCredential)
        .filter(
            UniversalAccessCredential.member_id == owner.id,
            UniversalAccessCredential.enabled.is_(True),
        )
        .all()
    )


def universal_primary_zones(
    db: Session,
    owner: Owner,
    *,
    already_ids: set[int],
) -> list[Zone]:
    """Primary zones in the member's enabled places, including other networks."""
    if is_system_administrator(owner):
        return []
    credentials = enabled_credentials_for(db, owner)
    if not credentials:
        return []
    rows = (
        db.query(Zone)
        .options(defer(Zone.geo_fence_polygon))
        .filter(Zone.active.is_(True), Zone.is_primary.is_(True))
        .order_by(Zone.id.desc())
        .limit(500)
        .all()
    )
    matched: list[Zone] = []
    for zone in rows:
        if int(zone.id) in already_ids:
            continue
        if any(_zone_in_credential(cred, zone) for cred in credentials):
            setattr(zone, "shared_via_universal", True)
            matched.append(zone)
            if len(matched) >= _MAX_UNIVERSAL_ZONES:
                break
    return matched
