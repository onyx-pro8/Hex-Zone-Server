"""Universal access codes: share selected primary zones with an invitee."""
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
    UniversalAccessRequest,
    UniversalAccessStatus,
)
from app.models.zone import Zone
from app.models.zone_message_event import ZoneMessageEvent
from app.services.access_policy import zone_listing_owner_ids
from app.services.account_type_policy import is_system_administrator
from app.services.registration_code_service import normalize_pricing_tier_key

logger = logging.getLogger(__name__)

_MAX_PHOTO_CHARS = 1_500_000
_MAX_UNIVERSAL_ZONES = 50
_SHARE_ACCOUNT_TYPES = frozenset({"enhanced", "enhanced_plus"})


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
    tier = normalize_pricing_tier_key(str(owner.account_type.value or ""))
    if tier not in _SHARE_ACCOUNT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Individual Pro and Organization administrators can request a universal access code.",
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


def _zone_summary(zone: Zone) -> dict[str, Any]:
    zone_type = zone.zone_type.value if hasattr(zone.zone_type, "value") else str(zone.zone_type)
    return {"id": int(zone.id), "name": zone.name, "zone_type": zone_type}


def shareable_primary_zones(db: Session, *, admin: Owner) -> list[dict[str, Any]]:
    _require_network_admin(admin)
    owner_ids = [int(item) for item in zone_listing_owner_ids(db, admin)]
    network_id = (admin.zone_id or "").strip()
    owned = []
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
        .filter(
            Zone.active.is_(True),
            Zone.is_primary.is_(True),
            or_(*owned),
        )
        .order_by(Zone.name.asc(), Zone.id.asc())
        .all()
    )
    return [_zone_summary(row) for row in rows]


def search_invitees(db: Session, *, admin: Owner, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Up to five active accounts whose email or name contains the keyword."""
    _require_network_admin(admin)
    needle = " ".join(str(query or "").split()).lower()
    if len(needle) < 1:
        return []
    like = f"%{needle}%"
    rows = (
        db.query(Owner)
        .filter(
            Owner.active.is_(True),
            Owner.id != admin.id,
            or_(
                func.lower(Owner.email).like(like),
                func.lower(Owner.first_name).like(like),
                func.lower(Owner.last_name).like(like),
            ),
        )
        .order_by(Owner.email.asc())
        .limit(max(1, min(int(limit), 5)))
        .all()
    )
    return [
        {"id": row.id, "name": _owner_name(row), "email": row.email}
        for row in rows
    ]


def _selected_primary_zones(db: Session, admin: Owner, zone_ids: list[int]) -> list[Zone]:
    selected = _int_ids(zone_ids)
    if not selected:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Select at least one primary zone to share.",
        )
    if len(selected) > _MAX_UNIVERSAL_ZONES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Select at most {_MAX_UNIVERSAL_ZONES} primary zones.",
        )
    allowed = {int(item["id"]) for item in shareable_primary_zones(db, admin=admin)}
    if any(zone_id not in allowed for zone_id in selected):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Every shared zone must be one of your active primary zones.",
        )
    return _zones_by_ids(db, selected)


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
    zone_ids = _int_ids(row.zone_ids)
    zones = [_zone_summary(zone) for zone in _zones_by_ids(db, zone_ids)]
    zone_label = ", ".join(zone["name"] for zone in zones)
    payload: dict[str, Any] = {
        "id": row.id,
        "member_id": row.member_id,
        "member_name": row.member_name,
        "email": row.email,
        "address": row.address or "",
        "country": row.country or "",
        "region": row.region or "",
        "city": row.city or "",
        "place": zone_label,
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
    if include_photo:
        payload["photo_id"] = row.photo_id
        payload["business_document"] = row.business_document or ""
    return payload


def create_request(
    db: Session,
    *,
    admin: Owner,
    invitee_email: str,
    zone_ids: list[int],
    photo_id: str,
    business_document: str,
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
    document = _image_document(business_document, label="Business document")
    zones = _selected_primary_zones(db, admin, zone_ids)
    invitee = owner_crud.get_owner_by_email(db, invitee_email)
    if invitee is None or not invitee.active or int(invitee.id) == int(admin.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active account uses that email.",
        )

    row = UniversalAccessRequest(
        network_admin_id=admin.id,
        member_id=invitee.id,
        member_name=_owner_name(invitee),
        email=invitee.email,
        address="",
        photo_id=photo,
        business_document=document,
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
    zone_label = ", ".join(zone.name for zone in _zones_by_ids(db, _int_ids(row.zone_ids))) or "selected zones"
    text = f"Universal access code requested for {row.member_name} ({zone_label})."
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
            "zone_ids": _int_ids(row.zone_ids),
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


async def notify_event_recipients(
    db: Session,
    event_id: str,
    *,
    title: str,
    flow: str,
) -> None:
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
            "metadata": {
                "sender_network_id": zone,
                "flow": flow,
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
        logger.exception("Universal access notice failed for event %s", event_id)


async def notify_system_admins_of_request(db: Session, event_id: str) -> None:
    await notify_event_recipients(
        db,
        event_id,
        title="Universal access request",
        flow="universal_access_request",
    )


async def notify_grant_recipients(db: Session, event_id: str) -> None:
    await notify_event_recipients(
        db,
        event_id,
        title="Universal access approved",
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
    zone_ids = _int_ids(row.zone_ids)
    credential = UniversalAccessCredential(
        request_id=row.id,
        member_id=row.member_id,
        network_admin_id=row.network_admin_id,
        code=_new_code(db),
        zone_ids=zone_ids,
        country="",
        region="",
        city="",
        enabled=True,
        place_bbox=None,
    )
    row.status = UniversalAccessStatus.APPROVED
    row.reviewed_by_id = caller.id
    row.reviewed_at = datetime.utcnow()
    row.reject_reason = ""
    db.add(credential)
    db.flush()
    notice = _record_grant_notice(db, row, caller)
    db.commit()
    db.refresh(row)
    payload = _request_row(db, row, include_photo=True)
    if notice is not None:
        payload["notice_event_id"] = notice.id
    return payload


def _record_grant_notice(
    db: Session,
    row: UniversalAccessRequest,
    reviewer: Owner,
) -> ZoneMessageEvent | None:
    """Tell the network admin and the invited user that the share was approved."""
    targets: list[int] = []
    for owner_id in (row.network_admin_id, row.member_id):
        if owner_id is None:
            continue
        value = int(owner_id)
        if value not in targets:
            targets.append(value)
    if not targets:
        return None
    zone_label = ", ".join(zone.name for zone in _zones_by_ids(db, _int_ids(row.zone_ids))) or "the shared zones"
    text = f"Universal access approved for {row.member_name}. They can now see {zone_label}."
    admin = db.get(Owner, row.network_admin_id)
    event = ZoneMessageEvent(
        zone_id=((admin.zone_id if admin else "") or "").strip() or "universal-access",
        sender_id=reviewer.id,
        receiver_id=int(row.member_id),
        type=CanonicalMessageType.PERMISSION.value,
        category=type_category(CanonicalMessageType.PERMISSION),
        scope=type_scope(CanonicalMessageType.PERMISSION),
        text=text,
        body_json={
            "code": "UNIVERSAL_ACCESS_APPROVED",
            "universal_access_request_id": row.id,
            "member_id": row.member_id,
            "member_name": row.member_name,
            "zone_ids": _int_ids(row.zone_ids),
        },
        metadata_json={
            "flow": "universal_access_granted",
            "permission_visibility": "direct",
            "universal_access_request_id": row.id,
            "delivered_owner_ids": targets,
            "sender_message": {"code": "UNIVERSAL_ACCESS_APPROVED", "text": text},
            "member_message": {"code": "UNIVERSAL_ACCESS_APPROVED", "text": text},
        },
    )
    db.add(event)
    db.flush()
    return event


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
    """Primary zones an approved universal access credential shares with this person."""
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
        .filter(
            Zone.id.in_(tuple(wanted)),
            Zone.active.is_(True),
            Zone.is_primary.is_(True),
        )
        .all()
    )
    by_id = {int(row.id): row for row in rows}
    matched: list[Zone] = []
    for zone_id in wanted:
        zone = by_id.get(zone_id)
        if zone is None:
            continue
        setattr(zone, "shared_via_universal", True)
        matched.append(zone)
    return matched
