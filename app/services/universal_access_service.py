"""Universal access: invite people into my zones, or ask to enter other zones."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, defer

from app.crud import owner as owner_crud
from app.domain.message_types import CanonicalMessageType, type_category, type_scope
from app.models.owner import Owner, OwnerRole
from app.models.universal_access import (
    UniversalAccessCredential,
    UniversalAccessKind,
    UniversalAccessRequest,
    UniversalAccessStatus,
)
from app.models.zone import Zone
from app.models.zone_message_event import ZoneMessageEvent
from app.services.access_policy import zone_listing_owner_ids
from app.services.account_type_policy import is_system_administrator
from app.services.communal_zone_service import get_communal_ids

logger = logging.getLogger(__name__)

_MAX_PHOTO_CHARS = 1_500_000
_MAX_UNIVERSAL_ZONES = 50


def _clean(value: str, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _require_network_admin(owner: Owner) -> None:
    if is_system_administrator(owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System administrators review universal access requests. They do not file them.",
        )
    if (owner.role.value or "").strip().lower() != "administrator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a network administrator can file a universal access request.",
        )


def _int_ids(raw: object) -> list[int]:
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value < 1 or value in seen:
            continue
        seen.add(value)
        ids.append(value)
    return ids


def _image_document(value: str, *, label: str) -> str:
    document = str(value or "").strip()
    if not document.startswith("data:image/") and not document.startswith("data:application/pdf"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{label} must be an image or PDF.",
        )
    if len(document) > _MAX_PHOTO_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{label} is too large.",
        )
    return document


def _owner_name(owner: Owner | None) -> str:
    if owner is None:
        return ""
    full = f"{owner.first_name} {owner.last_name}".strip()
    return full or owner.email


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


def _zone_config(zone: Zone) -> dict[str, Any]:
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    return config if isinstance(config, dict) else {}


def _zone_is_shareable(zone: Zone) -> bool:
    """Primary zones and communal-tagged zones. Secondary-only zones are out."""
    if bool(getattr(zone, "is_primary", False)):
        return True
    return bool(get_communal_ids(zone))


def _zone_summary(zone: Zone) -> dict[str, Any]:
    zone_type = zone.zone_type.value if hasattr(zone.zone_type, "value") else str(zone.zone_type)
    return {
        "id": int(zone.id),
        "name": zone.name,
        "zone_type": zone_type,
        "is_primary": bool(getattr(zone, "is_primary", False)),
        "communal": bool(get_communal_ids(zone)),
        "network_id": (zone.zone_id or "").strip(),
        "owner_id": int(zone.owner_id),
    }


def _zones_by_ids(db: Session, zone_ids: list[int]) -> list[Zone]:
    if not zone_ids:
        return []
    rows = (
        db.query(Zone)
        .options(defer(Zone.geo_fence_polygon))
        .filter(Zone.id.in_(tuple(zone_ids)))
        .all()
    )
    by_id = {int(row.id): row for row in rows}
    return [by_id[zone_id] for zone_id in zone_ids if zone_id in by_id]


def _request_kind(row: UniversalAccessRequest) -> UniversalAccessKind:
    kind = getattr(row, "request_kind", None)
    if isinstance(kind, UniversalAccessKind):
        return kind
    text = str(kind or UniversalAccessKind.INVITE.value).strip().lower()
    if text == UniversalAccessKind.PLACE_ACCESS.value:
        return UniversalAccessKind.PLACE_ACCESS
    return UniversalAccessKind.INVITE


def shareable_network_zones(db: Session, *, admin: Owner) -> list[dict[str, Any]]:
    """Zones this admin may invite someone into: primary + communal, not secondary-only."""
    _require_network_admin(admin)
    owner_ids = [int(item) for item in zone_listing_owner_ids(db, admin)]
    network_id = (admin.zone_id or "").strip()
    owned: list[Any] = []
    if owner_ids:
        owned.append(Zone.owner_id.in_(tuple(owner_ids)))
        owned.append(Zone.creator_id.in_(tuple(owner_ids)))
    if network_id:
        owned.append(Zone.zone_id == network_id)
    if not owned:
        return []
    rows = (
        db.query(Zone)
        .options(defer(Zone.geo_fence_polygon))
        .filter(Zone.active.is_(True), or_(*owned))
        .order_by(Zone.name.asc(), Zone.id.asc())
        .all()
    )
    return [_zone_summary(row) for row in rows if _zone_is_shareable(row)]


def search_invitees(db: Session, *, admin: Owner, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Up to five accounts in any network whose email or name contains the keyword."""
    _require_network_admin(admin)
    needle = " ".join(str(query or "").split()).lower()
    if len(needle) < 2:
        return []
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    full_name = func.lower(func.concat(Owner.first_name, " ", Owner.last_name))
    rows = (
        db.query(Owner)
        .filter(
            Owner.id != admin.id,
            or_(
                func.lower(Owner.email).like(like, escape="\\"),
                func.lower(Owner.first_name).like(like, escape="\\"),
                func.lower(Owner.last_name).like(like, escape="\\"),
                func.lower(Owner.broadcast_name).like(like, escape="\\"),
                full_name.like(like, escape="\\"),
            ),
        )
        .order_by(Owner.email.asc())
        .limit(max(1, min(int(limit), 5)))
        .all()
    )
    return [
        {
            "id": row.id,
            "name": _owner_name(row),
            "email": row.email,
            "network_id": (row.zone_id or "").strip(),
        }
        for row in rows
    ]


def _selected_shareable_zones(db: Session, admin: Owner, zone_ids: list[int]) -> list[Zone]:
    selected = _int_ids(zone_ids)
    if not selected:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Select at least one zone to share.",
        )
    if len(selected) > _MAX_UNIVERSAL_ZONES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Select at most {_MAX_UNIVERSAL_ZONES} zones.",
        )
    allowed = {int(item["id"]) for item in shareable_network_zones(db, admin=admin)}
    if any(zone_id not in allowed for zone_id in selected):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Every shared zone must be a primary or communal zone on your network.",
        )
    return _zones_by_ids(db, selected)


def _place_label(country: str, region: str, city: str) -> str:
    return ", ".join(part for part in (city, region, country) if part)


def _normalize_place(country: str, region: str, city: str) -> tuple[str, str, str]:
    country_v = _clean(country, limit=120)
    region_v = _clean(region, limit=120)
    city_v = _clean(city, limit=120)
    if not country_v and not region_v and not city_v:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter at least a country, state/province, or city.",
        )
    if region_v and not country_v:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose a country before the state or province.",
        )
    if city_v and not country_v:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose a country before the city.",
        )
    return country_v, region_v, city_v


def _centroid(zone: Zone) -> tuple[float, float] | None:
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    polygon = geometry.get("geo_fence_polygon") if isinstance(geometry, dict) else None
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
    try:
        from app.services.area_boundary_service import (
            AreaLocationInput,
            lookup_global_area_boundary,
        )

        parts = [part for part in (city, region) if part]
        location = AreaLocationInput(country=country or region or city, city=", ".join(parts))
        found = lookup_global_area_boundary(location)
    except Exception:
        return None
    if not found:
        return None
    polygon, _name, _config = found
    return _bbox_from_polygon(polygon if isinstance(polygon, dict) else None)


def _text_place_match(zone: Zone, *, country: str, region: str, city: str) -> bool:
    config = _zone_config(zone)
    zone_city = str(config.get("city") or "").strip().lower()
    zone_region = str(config.get("region") or config.get("state") or "").strip().lower()
    zone_country = str(config.get("country") or "").strip().lower()
    if country and zone_country and zone_country != country.lower():
        return False
    if region and zone_region and zone_region != region.lower():
        return False
    if city and zone_city and zone_city != city.lower():
        return False
    if country and not zone_country and not zone_region and not zone_city:
        return False
    if country and zone_country and zone_country == country.lower():
        if region and zone_region and zone_region != region.lower():
            return False
        if city and zone_city and zone_city != city.lower():
            return False
        return True
    if region and zone_region and zone_region == region.lower():
        return True
    if city and zone_city and zone_city == city.lower():
        return True
    return False


def _bbox_place_match(zone: Zone, bbox: list[float] | None) -> bool:
    if not isinstance(bbox, list) or len(bbox) != 4:
        return False
    centroid = _centroid(zone)
    if centroid is None:
        return False
    lng, lat = centroid
    west, south, east, north = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    return west <= lng <= east and south <= lat <= north


def place_candidate_zones(
    db: Session,
    *,
    country: str,
    region: str,
    city: str,
) -> list[dict[str, Any]]:
    """Non-secondary zones that match or contain the requested place."""
    country_v, region_v, city_v = _normalize_place(country, region, city)
    bbox = _resolve_place_bbox(country_v, region_v, city_v)
    rows = (
        db.query(Zone)
        .options(defer(Zone.geo_fence_polygon))
        .filter(Zone.active.is_(True))
        .order_by(Zone.name.asc(), Zone.id.asc())
        .limit(800)
        .all()
    )
    matched: list[dict[str, Any]] = []
    for zone in rows:
        if not _zone_is_shareable(zone):
            continue
        if _text_place_match(zone, country=country_v, region=region_v, city=city_v) or _bbox_place_match(
            zone, bbox
        ):
            matched.append(_zone_summary(zone))
            if len(matched) >= _MAX_UNIVERSAL_ZONES:
                break
    return matched


def _request_row(
    db: Session,
    row: UniversalAccessRequest,
    *,
    include_docs: bool,
) -> dict[str, Any]:
    admin = db.get(Owner, row.network_admin_id)
    credential = (
        db.query(UniversalAccessCredential)
        .filter(UniversalAccessCredential.request_id == row.id)
        .first()
    )
    kind = _request_kind(row)
    zone_ids = _int_ids(row.zone_ids)
    zones = [_zone_summary(zone) for zone in _zones_by_ids(db, zone_ids)]
    zone_label = ", ".join(zone["name"] for zone in zones)
    place = _place_label(row.country or "", row.region or "", row.city or "")
    payload: dict[str, Any] = {
        "id": row.id,
        "request_kind": kind.value,
        "member_id": row.member_id,
        "member_name": row.member_name,
        "email": row.email,
        "address": row.address or "",
        "country": row.country or "",
        "region": row.region or "",
        "city": row.city or "",
        "place": place or zone_label,
        "zone_ids": zone_ids,
        "zones": zones,
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
    if kind == UniversalAccessKind.PLACE_ACCESS and row.status == UniversalAccessStatus.PENDING:
        payload["candidate_zones"] = place_candidate_zones(
            db,
            country=row.country or "",
            region=row.region or "",
            city=row.city or "",
        )
    if include_docs:
        payload["photo_id"] = row.photo_id or ""
        payload["business_document"] = row.business_document or ""
    return payload


def create_invite_request(
    db: Session,
    *,
    admin: Owner,
    invitee_email: str,
    zone_ids: list[int],
    note: str = "",
) -> dict[str, Any]:
    _require_network_admin(admin)
    zones = _selected_shareable_zones(db, admin, zone_ids)
    invitee = owner_crud.get_owner_by_email(db, invitee_email)
    if invitee is None or int(invitee.id) == int(admin.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account uses that email.",
        )

    row = UniversalAccessRequest(
        network_admin_id=admin.id,
        member_id=invitee.id,
        member_name=_owner_name(invitee),
        email=invitee.email,
        address="",
        photo_id="",
        business_document="",
        request_kind=UniversalAccessKind.INVITE,
        zone_ids=[int(zone.id) for zone in zones],
        country="",
        region="",
        city="",
        note=_clean(note, limit=1000),
        status=UniversalAccessStatus.PENDING,
    )
    db.add(row)
    db.flush()
    notice = _record_request_notice(db, row, admin)
    db.commit()
    db.refresh(row)
    payload = _request_row(db, row, include_docs=True)
    if notice is not None:
        payload["notice_event_id"] = notice.id
    return payload


def create_place_access_request(
    db: Session,
    *,
    admin: Owner,
    country: str,
    region: str,
    city: str,
    photo_id: str,
    business_document: str,
    note: str = "",
) -> dict[str, Any]:
    _require_network_admin(admin)
    country_v, region_v, city_v = _normalize_place(country, region, city)
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
    document = _image_document(business_document, label="Business document")

    row = UniversalAccessRequest(
        network_admin_id=admin.id,
        member_id=admin.id,
        member_name=_owner_name(admin),
        email=admin.email,
        address="",
        photo_id=photo,
        business_document=document,
        request_kind=UniversalAccessKind.PLACE_ACCESS,
        zone_ids=[],
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
    payload = _request_row(db, row, include_docs=True)
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
    targets = _system_admin_ids(db)
    if not targets:
        return None
    kind = _request_kind(row)
    if kind == UniversalAccessKind.INVITE:
        zone_label = ", ".join(zone.name for zone in _zones_by_ids(db, _int_ids(row.zone_ids))) or "selected zones"
        text = f"Universal access invite requested for {row.member_name} ({zone_label})."
        code = "UNIVERSAL_ACCESS_INVITE_REQUESTED"
    else:
        place = _place_label(row.country or "", row.region or "", row.city or "")
        text = f"Universal place access requested by {_owner_name(admin)} ({place})."
        code = "UNIVERSAL_ACCESS_PLACE_REQUESTED"
    event = ZoneMessageEvent(
        zone_id=(admin.zone_id or "").strip() or "universal-access",
        sender_id=admin.id,
        receiver_id=targets[0],
        type=CanonicalMessageType.PERMISSION.value,
        category=type_category(CanonicalMessageType.PERMISSION),
        scope=type_scope(CanonicalMessageType.PERMISSION),
        text=text,
        body_json={
            "code": code,
            "universal_access_request_id": row.id,
            "request_kind": kind.value,
            "member_id": row.member_id,
            "member_name": row.member_name,
            "zone_ids": _int_ids(row.zone_ids),
            "note": row.note or "",
        },
        metadata_json={
            "flow": "universal_access_request",
            "permission_visibility": "direct",
            "universal_access_request_id": row.id,
            "delivered_owner_ids": targets,
            "sender_message": {"code": code, "text": text},
            "member_message": {"code": code, "text": text},
        },
    )
    db.add(event)
    db.flush()
    return event


async def notify_event_recipients(
    db: Session,
    event_id: str,
    *,
    title: str,
    flow: str,
) -> None:
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
            title=title,
            body=(event.text or title)[:240],
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
            "metadata": {"sender_network_id": zone, "flow": flow},
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
        logger.exception("Universal access notice failed for event %s", event_id)


async def notify_system_admins_of_request(db: Session, event_id: str) -> None:
    await notify_event_recipients(
        db,
        event_id,
        title="Universal access request",
        flow="universal_access_request",
    )


async def notify_decision_recipients(db: Session, event_id: str, *, title: str) -> None:
    await notify_event_recipients(
        db,
        event_id,
        title=title,
        flow="universal_access_granted",
    )


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
    include_docs = is_system_administrator(caller)
    return [_request_row(db, row, include_docs=include_docs) for row in rows]


def get_request(db: Session, *, caller: Owner, request_id: int) -> dict[str, Any]:
    row = db.get(UniversalAccessRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    if not is_system_administrator(caller) and int(row.network_admin_id) != int(caller.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request not found.")
    if (caller.role.value or "").strip().lower() != "administrator":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request not found.")
    return _request_row(db, row, include_docs=True)


def approve_request(
    db: Session,
    *,
    caller: Owner,
    request_id: int,
    zone_ids: list[int] | None = None,
) -> dict[str, Any]:
    if not is_system_administrator(caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the system administrator can approve a universal access request.",
        )
    row = db.get(UniversalAccessRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    if row.status != UniversalAccessStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request was already reviewed.",
        )
    kind = _request_kind(row)
    if kind == UniversalAccessKind.INVITE:
        granted = _int_ids(row.zone_ids)
    else:
        granted = _int_ids(zone_ids or [])
        if not granted:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Select at least one matching zone to approve.",
            )
        candidates = {
            int(item["id"])
            for item in place_candidate_zones(
                db,
                country=row.country or "",
                region=row.region or "",
                city=row.city or "",
            )
        }
        if any(zone_id not in candidates for zone_id in granted):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Every approved zone must match the requested place.",
            )
        row.zone_ids = granted

    credential = UniversalAccessCredential(
        request_id=row.id,
        member_id=row.member_id,
        network_admin_id=row.network_admin_id,
        code=_new_code(db),
        zone_ids=granted,
        country=row.country or "",
        region=row.region or "",
        city=row.city or "",
        enabled=True,
        place_bbox=None,
    )
    row.status = UniversalAccessStatus.APPROVED
    row.reviewed_by_id = caller.id
    row.reviewed_at = datetime.utcnow()
    row.reject_reason = ""
    db.add(credential)
    db.flush()
    notice = _record_decision_notice(db, row, caller, approved=True)
    db.commit()
    db.refresh(row)
    payload = _request_row(db, row, include_docs=True)
    if notice is not None:
        payload["notice_event_id"] = notice.id
    return payload


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
            detail="Only the system administrator can reject a universal access request.",
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
    row.status = UniversalAccessStatus.REJECTED
    row.reject_reason = cleaned
    row.reviewed_by_id = caller.id
    row.reviewed_at = datetime.utcnow()
    db.flush()
    notice = _record_decision_notice(db, row, caller, approved=False)
    db.commit()
    db.refresh(row)
    payload = _request_row(db, row, include_docs=True)
    if notice is not None:
        payload["notice_event_id"] = notice.id
    return payload


def _zone_admin_ids(db: Session, zone_ids: list[int]) -> list[int]:
    targets: list[int] = []
    seen: set[int] = set()
    for zone in _zones_by_ids(db, zone_ids):
        network = (zone.zone_id or "").strip()
        if not network:
            continue
        admins = (
            db.query(Owner)
            .filter(
                Owner.zone_id == network,
                Owner.role == OwnerRole.ADMINISTRATOR,
                Owner.active.is_(True),
            )
            .all()
        )
        for admin in admins:
            if is_system_administrator(admin):
                continue
            value = int(admin.id)
            if value in seen:
                continue
            seen.add(value)
            targets.append(value)
    return targets


def _record_decision_notice(
    db: Session,
    row: UniversalAccessRequest,
    reviewer: Owner,
    *,
    approved: bool,
) -> ZoneMessageEvent | None:
    kind = _request_kind(row)
    targets: list[int] = []
    for owner_id in (row.network_admin_id, row.member_id):
        if owner_id is None:
            continue
        value = int(owner_id)
        if value not in targets:
            targets.append(value)
    if approved and kind == UniversalAccessKind.PLACE_ACCESS:
        for admin_id in _zone_admin_ids(db, _int_ids(row.zone_ids)):
            if admin_id not in targets:
                targets.append(admin_id)
    if not targets:
        return None

    zone_label = ", ".join(zone.name for zone in _zones_by_ids(db, _int_ids(row.zone_ids))) or "the shared zones"
    if approved:
        text = f"Universal access approved for {row.member_name}. Access includes {zone_label}."
        code = "UNIVERSAL_ACCESS_APPROVED"
        flow = "universal_access_granted"
    else:
        reason = row.reject_reason or "No reason given."
        text = f"Universal access rejected for {row.member_name}. {reason}"
        code = "UNIVERSAL_ACCESS_REJECTED"
        flow = "universal_access_granted"
    admin = db.get(Owner, row.network_admin_id)
    event = ZoneMessageEvent(
        zone_id=((admin.zone_id if admin else "") or "").strip() or "universal-access",
        sender_id=reviewer.id,
        receiver_id=int(row.network_admin_id),
        type=CanonicalMessageType.PERMISSION.value,
        category=type_category(CanonicalMessageType.PERMISSION),
        scope=type_scope(CanonicalMessageType.PERMISSION),
        text=text,
        body_json={
            "code": code,
            "universal_access_request_id": row.id,
            "request_kind": kind.value,
            "member_id": row.member_id,
            "member_name": row.member_name,
            "zone_ids": _int_ids(row.zone_ids),
        },
        metadata_json={
            "flow": flow,
            "permission_visibility": "direct",
            "universal_access_request_id": row.id,
            "delivered_owner_ids": targets,
            "sender_message": {"code": code, "text": text},
            "member_message": {"code": code, "text": text},
        },
    )
    db.add(event)
    db.flush()
    return event


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
    return _request_row(db, request_row, include_docs=is_system_administrator(caller))


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
    """Zones an approved universal access credential shares with this person."""
    if is_system_administrator(owner):
        return []
    wanted: list[int] = []
    seen: set[int] = set()
    for credential in enabled_credentials_for(db, owner):
        for zone_id in _int_ids(credential.zone_ids):
            if zone_id in seen or zone_id in already_ids:
                continue
            seen.add(zone_id)
            wanted.append(zone_id)
            if len(wanted) >= _MAX_UNIVERSAL_ZONES:
                break
        if len(wanted) >= _MAX_UNIVERSAL_ZONES:
            break
    if not wanted:
        return []
    rows = (
        db.query(Zone)
        .options(defer(Zone.geo_fence_polygon))
        .filter(Zone.id.in_(tuple(wanted)), Zone.active.is_(True))
        .all()
    )
    by_id = {int(row.id): row for row in rows}
    matched: list[Zone] = []
    for zone_id in wanted:
        zone = by_id.get(zone_id)
        if zone is None or not _zone_is_shareable(zone):
            continue
        setattr(zone, "shared_via_universal", True)
        matched.append(zone)
    return matched


# Backward-compatible aliases used by older call sites / tests.
shareable_primary_zones = shareable_network_zones


def create_request(
    db: Session,
    *,
    admin: Owner,
    invitee_email: str,
    zone_ids: list[int],
    photo_id: str = "",
    business_document: str = "",
    note: str = "",
) -> dict[str, Any]:
    return create_invite_request(
        db,
        admin=admin,
        invitee_email=invitee_email,
        zone_ids=zone_ids,
        note=note,
    )
