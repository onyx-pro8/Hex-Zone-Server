"""Universal access: generate private/public keys, request access to other networks."""
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
    UniversalAccessActivation,
    UniversalAccessCredential,
    UniversalAccessKeyType,
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
            detail="System administrators are not part of the universal access workflow.",
        )
    if (owner.role.value or "").strip().lower() != "administrator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a network administrator can generate universal access keys.",
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


def _photo_id(value: str) -> str:
    photo = str(value or "").strip()
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
    return photo


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


def _zone_is_shareable(zone: Zone) -> bool:
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


def _credential_key_type(credential: UniversalAccessCredential | None) -> str:
    if credential is None:
        return UniversalAccessKeyType.PRIVATE.value
    raw = getattr(credential, "key_type", None)
    if isinstance(raw, UniversalAccessKeyType):
        return raw.value
    text = str(raw or UniversalAccessKeyType.PRIVATE.value).strip().lower()
    if text == UniversalAccessKeyType.PUBLIC.value:
        return UniversalAccessKeyType.PUBLIC.value
    return UniversalAccessKeyType.PRIVATE.value


def _network_admins(db: Session, network_id: str) -> list[Owner]:
    network = (network_id or "").strip()
    if not network:
        return []
    rows = (
        db.query(Owner)
        .filter(
            Owner.zone_id == network,
            Owner.role == OwnerRole.ADMINISTRATOR,
            Owner.active.is_(True),
        )
        .order_by(Owner.id.asc())
        .all()
    )
    return [row for row in rows if not is_system_administrator(row)]


def _primary_network_admin(db: Session, network_id: str) -> Owner:
    admins = _network_admins(db, network_id)
    if not admins:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No network administrator found for that network ID.",
        )
    return admins[0]


def shareable_network_zones(db: Session, *, admin: Owner) -> list[dict[str, Any]]:
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


def lookup_target_network(db: Session, *, caller: Owner, network_id: str) -> dict[str, Any]:
    """Network admin email + shareable zones for Access other."""
    del caller  # any authenticated user may look up a target network
    network = _clean(network_id, limit=100)
    if not network:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a network ID.",
        )
    admin = _primary_network_admin(db, network)
    zones = (
        db.query(Zone)
        .options(defer(Zone.geo_fence_polygon))
        .filter(Zone.active.is_(True), Zone.zone_id == network)
        .order_by(Zone.name.asc(), Zone.id.asc())
        .all()
    )
    shareable = [_zone_summary(zone) for zone in zones if _zone_is_shareable(zone)]
    return {
        "network_id": network,
        "admin_id": int(admin.id),
        "admin_email": admin.email,
        "admin_name": _owner_name(admin),
        "zones": shareable,
    }


def search_target_networks(db: Session, *, caller: Owner, query: str, limit: int = 8) -> list[dict[str, Any]]:
    needle = " ".join(str(query or "").split()).lower()
    if len(needle) < 2:
        return []
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    zone_rows = (
        db.query(Zone)
        .options(defer(Zone.geo_fence_polygon))
        .filter(
            Zone.active.is_(True),
            or_(
                func.lower(Zone.zone_id).like(like, escape="\\"),
                func.lower(Zone.name).like(like, escape="\\"),
            ),
        )
        .order_by(Zone.zone_id.asc(), Zone.name.asc())
        .limit(40)
        .all()
    )
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for zone in zone_rows:
        if not _zone_is_shareable(zone):
            continue
        network = (zone.zone_id or "").strip()
        if not network or network in seen:
            continue
        try:
            payload = lookup_target_network(db, caller=caller, network_id=network)
        except HTTPException:
            continue
        seen.add(network)
        results.append(payload)
        if len(results) >= max(1, min(int(limit), 8)):
            break
    return results


def _viewer_actions(
    *,
    caller: Owner,
    row: UniversalAccessRequest,
    kind: UniversalAccessKind,
) -> dict[str, bool]:
    is_admin = int(row.network_admin_id) == int(caller.id)
    is_member = int(row.member_id) == int(caller.id)
    pending = row.status == UniversalAccessStatus.PENDING
    if kind == UniversalAccessKind.PLACE_ACCESS:
        return {
            "can_approve": is_admin and pending,
            "can_reject": is_admin and pending,
            "can_edit_docs": is_member and pending,
            "can_remove": (is_member and pending) or (is_admin and pending),
            "can_view_detail": True,
        }
    # Invite / generated keys: admin may remove; invitee is read-only.
    return {
        "can_approve": False,
        "can_reject": False,
        "can_edit_docs": False,
        "can_remove": is_admin,
        "can_view_detail": True,
    }


def _request_row(
    db: Session,
    row: UniversalAccessRequest,
    *,
    caller: Owner | None,
    include_docs: bool,
) -> dict[str, Any]:
    admin = db.get(Owner, row.network_admin_id)
    member = db.get(Owner, row.member_id)
    credential = (
        db.query(UniversalAccessCredential)
        .filter(UniversalAccessCredential.request_id == row.id)
        .first()
    )
    kind = _request_kind(row)
    zone_ids = _int_ids(row.zone_ids)
    zones = [_zone_summary(zone) for zone in _zones_by_ids(db, zone_ids)]
    zone_label = ", ".join(zone["name"] for zone in zones)
    key_type = _credential_key_type(credential)
    viewer_role = "other"
    if caller is not None:
        if int(row.network_admin_id) == int(caller.id) and kind == UniversalAccessKind.INVITE:
            viewer_role = "owner"
        elif int(row.network_admin_id) == int(caller.id):
            viewer_role = "reviewer"
        elif int(row.member_id) == int(caller.id) and kind == UniversalAccessKind.PLACE_ACCESS:
            viewer_role = "requester"
        elif int(row.member_id) == int(caller.id):
            viewer_role = "invitee"
    payload: dict[str, Any] = {
        "id": row.id,
        "request_kind": kind.value,
        "key_type": key_type,
        "member_id": row.member_id,
        "member_name": row.member_name,
        "first_name": (member.first_name if member else "") or "",
        "last_name": (member.last_name if member else "") or "",
        "email": row.email,
        "address": row.address or (member.address if member else "") or "",
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
        "network_admin_email": admin.email if admin else "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "code": credential.code if credential else None,
        "credential_id": credential.id if credential else None,
        "enabled": credential.enabled if credential else None,
        "viewer_role": viewer_role,
        "viewer_actions": (
            _viewer_actions(caller=caller, row=row, kind=kind)
            if caller is not None
            else {
                "can_approve": False,
                "can_reject": False,
                "can_edit_docs": False,
                "can_remove": False,
                "can_view_detail": True,
            }
        ),
    }
    if include_docs:
        payload["photo_id"] = row.photo_id or ""
        payload["business_document"] = row.business_document or ""
    return payload


def _record_permission_notice(
    db: Session,
    *,
    row: UniversalAccessRequest,
    sender: Owner,
    targets: list[int],
    text: str,
    code: str,
    flow: str,
) -> ZoneMessageEvent | None:
    cleaned_targets: list[int] = []
    seen: set[int] = set()
    for owner_id in targets:
        value = int(owner_id)
        if value in seen:
            continue
        seen.add(value)
        cleaned_targets.append(value)
    if not cleaned_targets:
        return None
    kind = _request_kind(row)
    event = ZoneMessageEvent(
        zone_id=(sender.zone_id or "").strip() or "universal-access",
        sender_id=sender.id,
        receiver_id=cleaned_targets[0],
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
            "flow": flow,
            "permission_visibility": "direct",
            "universal_access_request_id": row.id,
            "delivered_owner_ids": cleaned_targets,
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
        # Smart-home webhook when a hub/device is connected for the recipient.
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


async def notify_permission_recipients(db: Session, event_id: str, *, title: str) -> None:
    await notify_event_recipients(
        db,
        event_id,
        title=title,
        flow="universal_access",
    )


# Backward-compatible names used by the router / older call sites.
async def notify_system_admins_of_request(db: Session, event_id: str) -> None:
    await notify_permission_recipients(db, event_id, title="Universal access request")


async def notify_decision_recipients(db: Session, event_id: str, *, title: str) -> None:
    await notify_permission_recipients(db, event_id, title=title)


async def notify_granted_zones(db: Session, *, member_id: int, zone_ids: list[int]) -> None:
    from app.websocket.manager import ws_manager

    owner = db.get(Owner, int(member_id))
    if owner is None:
        return
    visible = universal_primary_zones(db, owner, already_ids=set())
    try:
        from app.services.zone_service import _serialize_zone

        zones = [_serialize_zone(zone) for zone in visible]
    except Exception:
        logger.exception("Could not serialize granted zones for owner %s", member_id)
        zones = []
    try:
        await ws_manager.broadcast_to_users(
            [int(member_id)],
            "UNIVERSAL_ZONES_GRANTED",
            {
                "member_id": int(member_id),
                "zone_ids": _int_ids(zone_ids),
                "zones": zones,
            },
        )
    except Exception:
        logger.exception("Universal zone grant push failed for owner %s", member_id)


def generate_invite_key(
    db: Session,
    *,
    admin: Owner,
    key_type: str,
    zone_ids: list[int],
    invitee_email: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Network admin generates a private or public universal access key immediately."""
    _require_network_admin(admin)
    visibility = str(key_type or "").strip().lower()
    if visibility not in {UniversalAccessKeyType.PRIVATE.value, UniversalAccessKeyType.PUBLIC.value}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Key type must be private or public.",
        )
    zones = _selected_shareable_zones(db, admin, zone_ids)
    granted_ids = [int(zone.id) for zone in zones]

    if visibility == UniversalAccessKeyType.PRIVATE.value:
        invitee = owner_crud.get_owner_by_email(db, invitee_email)
        if invitee is None or int(invitee.id) == int(admin.id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No account uses that email.",
            )
        member = invitee
    else:
        member = admin

    row = UniversalAccessRequest(
        network_admin_id=admin.id,
        member_id=member.id,
        member_name=_owner_name(member),
        email=member.email,
        address=(member.address or ""),
        photo_id="",
        business_document="",
        request_kind=UniversalAccessKind.INVITE,
        zone_ids=granted_ids,
        country="",
        region="",
        city="",
        note=_clean(note, limit=1000),
        status=UniversalAccessStatus.APPROVED,
        reviewed_by_id=admin.id,
        reviewed_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    credential = UniversalAccessCredential(
        request_id=row.id,
        member_id=member.id,
        network_admin_id=admin.id,
        code=_new_code(db),
        key_type=(
            UniversalAccessKeyType.PRIVATE
            if visibility == UniversalAccessKeyType.PRIVATE.value
            else UniversalAccessKeyType.PUBLIC
        ),
        zone_ids=granted_ids,
        country="",
        region="",
        city="",
        enabled=True,
        place_bbox=None,
    )
    db.add(credential)
    db.flush()

    notice = None
    if visibility == UniversalAccessKeyType.PRIVATE.value:
        zone_label = ", ".join(zone.name for zone in zones) or "shared zones"
        notice = _record_permission_notice(
            db,
            row=row,
            sender=admin,
            targets=[int(member.id)],
            text=f"{_owner_name(admin)} shared universal access ({zone_label}). Key: {credential.code}",
            code="UNIVERSAL_ACCESS_INVITE_SHARED",
            flow="universal_access_invite",
        )

    db.commit()
    db.refresh(row)
    payload = _request_row(db, row, caller=admin, include_docs=False)
    if notice is not None:
        payload["notice_event_id"] = notice.id
    return payload


def create_invite_request(
    db: Session,
    *,
    admin: Owner,
    invitee_email: str,
    zone_ids: list[int],
    note: str = "",
    key_type: str = "private",
) -> dict[str, Any]:
    """Backward-compatible wrapper: generate a private (or public) invite key."""
    return generate_invite_key(
        db,
        admin=admin,
        key_type=key_type or "private",
        zone_ids=zone_ids,
        invitee_email=invitee_email,
        note=note,
    )


def create_place_access_request(
    db: Session,
    *,
    requester: Owner,
    network_id: str,
    zone_ids: list[int],
    photo_id: str,
    business_document: str,
    note: str = "",
) -> dict[str, Any]:
    if is_system_administrator(requester):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System administrators are not part of the universal access workflow.",
        )
    target = lookup_target_network(db, caller=requester, network_id=network_id)
    admin = db.get(Owner, int(target["admin_id"]))
    if admin is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Network admin not found.")
    if int(admin.id) == int(requester.id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="You already administer that network.",
        )
    selected = _int_ids(zone_ids)
    allowed = {int(item["id"]) for item in target["zones"]}
    if not selected:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Select at least one zone to request.",
        )
    if any(zone_id not in allowed for zone_id in selected):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Every requested zone must belong to that network.",
        )
    photo = _photo_id(photo_id)
    document = _image_document(business_document, label="Business document")

    row = UniversalAccessRequest(
        network_admin_id=admin.id,
        member_id=requester.id,
        member_name=_owner_name(requester),
        email=requester.email,
        address=(requester.address or ""),
        photo_id=photo,
        business_document=document,
        request_kind=UniversalAccessKind.PLACE_ACCESS,
        zone_ids=selected,
        country="",
        region="",
        city="",
        note=_clean(note, limit=1000),
        status=UniversalAccessStatus.PENDING,
    )
    db.add(row)
    db.flush()
    zone_label = ", ".join(zone["name"] for zone in target["zones"] if int(zone["id"]) in set(selected))
    notice = _record_permission_notice(
        db,
        row=row,
        sender=requester,
        targets=[int(admin.id)],
        text=f"{_owner_name(requester)} requested universal access to {zone_label or 'your zones'}.",
        code="UNIVERSAL_ACCESS_PLACE_REQUESTED",
        flow="universal_access_request",
    )
    db.commit()
    db.refresh(row)
    payload = _request_row(db, row, caller=requester, include_docs=True)
    if notice is not None:
        payload["notice_event_id"] = notice.id
    return payload


def list_requests(db: Session, *, caller: Owner) -> list[dict[str, Any]]:
    rows = (
        db.query(UniversalAccessRequest)
        .filter(
            or_(
                UniversalAccessRequest.network_admin_id == caller.id,
                UniversalAccessRequest.member_id == caller.id,
            )
        )
        .order_by(UniversalAccessRequest.created_at.desc(), UniversalAccessRequest.id.desc())
        .all()
    )
    # Public keys owned by the admin appear for the admin; invitees see private invites.
    # Skip public invite rows for the member when member_id was set to the admin (creator).
    visible: list[UniversalAccessRequest] = []
    for row in rows:
        kind = _request_kind(row)
        if kind == UniversalAccessKind.INVITE and int(row.member_id) == int(caller.id):
            credential = (
                db.query(UniversalAccessCredential)
                .filter(UniversalAccessCredential.request_id == row.id)
                .first()
            )
            if _credential_key_type(credential) == UniversalAccessKeyType.PUBLIC.value:
                # Creator is also member_id for public keys; still show once as owner.
                if int(row.network_admin_id) != int(caller.id):
                    continue
        visible.append(row)
    return [
        _request_row(
            db,
            row,
            caller=caller,
            include_docs=int(row.member_id) == int(caller.id)
            or int(row.network_admin_id) == int(caller.id),
        )
        for row in visible
    ]


def get_request(db: Session, *, caller: Owner, request_id: int) -> dict[str, Any]:
    row = db.get(UniversalAccessRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    if int(row.network_admin_id) != int(caller.id) and int(row.member_id) != int(caller.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request not found.")
    return _request_row(db, row, caller=caller, include_docs=True)


def update_request_docs(
    db: Session,
    *,
    caller: Owner,
    request_id: int,
    photo_id: str,
    business_document: str,
) -> dict[str, Any]:
    row = db.get(UniversalAccessRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    if _request_kind(row) != UniversalAccessKind.PLACE_ACCESS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only place-access requests have editable documents.",
        )
    if int(row.member_id) != int(caller.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request not found.")
    if row.status != UniversalAccessStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only pending requests can be edited.",
        )
    row.photo_id = _photo_id(photo_id)
    row.business_document = _image_document(business_document, label="Business document")
    db.commit()
    db.refresh(row)
    return _request_row(db, row, caller=caller, include_docs=True)


def delete_request(db: Session, *, caller: Owner, request_id: int) -> dict[str, Any]:
    row = db.get(UniversalAccessRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    kind = _request_kind(row)
    is_admin = int(row.network_admin_id) == int(caller.id)
    is_member = int(row.member_id) == int(caller.id)
    if kind == UniversalAccessKind.INVITE:
        if not is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the network admin can remove this access key.",
            )
    else:
        if row.status != UniversalAccessStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only pending requests can be removed.",
            )
        if not (is_member or is_admin):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request not found.")
    payload = _request_row(db, row, caller=caller, include_docs=False)
    credential = (
        db.query(UniversalAccessCredential)
        .filter(UniversalAccessCredential.request_id == row.id)
        .first()
    )
    if credential is not None:
        db.query(UniversalAccessActivation).filter(
            UniversalAccessActivation.credential_id == credential.id
        ).delete(synchronize_session=False)
        db.delete(credential)
    db.delete(row)
    db.commit()
    return {"ok": True, "id": payload["id"]}


def approve_request(
    db: Session,
    *,
    caller: Owner,
    request_id: int,
    zone_ids: list[int] | None = None,
) -> dict[str, Any]:
    del zone_ids  # place-access zones are fixed on the request
    _require_network_admin(caller)
    row = db.get(UniversalAccessRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    if int(row.network_admin_id) != int(caller.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request not found.")
    if row.status != UniversalAccessStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request was already reviewed.",
        )
    if _request_kind(row) != UniversalAccessKind.PLACE_ACCESS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only place-access requests need approval.",
        )
    granted = _int_ids(row.zone_ids)
    if not granted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This request has no zones to approve.",
        )
    credential = UniversalAccessCredential(
        request_id=row.id,
        member_id=row.member_id,
        network_admin_id=row.network_admin_id,
        code=_new_code(db),
        key_type=UniversalAccessKeyType.PRIVATE,
        zone_ids=granted,
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
    zone_label = ", ".join(zone.name for zone in _zones_by_ids(db, granted)) or "the shared zones"
    notice = _record_permission_notice(
        db,
        row=row,
        sender=caller,
        targets=[int(row.member_id)],
        text=f"Universal access approved. Access includes {zone_label}. Key: {credential.code}",
        code="UNIVERSAL_ACCESS_APPROVED",
        flow="universal_access_granted",
    )
    db.commit()
    db.refresh(row)
    payload = _request_row(db, row, caller=caller, include_docs=True)
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
    _require_network_admin(caller)
    row = db.get(UniversalAccessRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    if int(row.network_admin_id) != int(caller.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request not found.")
    if row.status != UniversalAccessStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request was already reviewed.",
        )
    if _request_kind(row) != UniversalAccessKind.PLACE_ACCESS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only place-access requests can be rejected.",
        )
    cleaned = _clean(reason, limit=500)
    row.status = UniversalAccessStatus.REJECTED
    row.reject_reason = cleaned
    row.reviewed_by_id = caller.id
    row.reviewed_at = datetime.utcnow()
    db.flush()
    reason_text = cleaned or "No reason given."
    notice = _record_permission_notice(
        db,
        row=row,
        sender=caller,
        targets=[int(row.member_id)],
        text=f"Universal access rejected. {reason_text}",
        code="UNIVERSAL_ACCESS_REJECTED",
        flow="universal_access_granted",
    )
    db.commit()
    db.refresh(row)
    payload = _request_row(db, row, caller=caller, include_docs=True)
    if notice is not None:
        payload["notice_event_id"] = notice.id
    return payload


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
    if int(credential.network_admin_id) != int(caller.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Credential not found.")
    if (caller.role.value or "").strip().lower() != "administrator":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Credential not found.")
    credential.enabled = bool(enabled)
    db.commit()
    request_row = db.get(UniversalAccessRequest, credential.request_id)
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    return _request_row(db, request_row, caller=caller, include_docs=False)


def list_private_keys(db: Session, *, caller: Owner) -> list[dict[str, Any]]:
    rows = (
        db.query(UniversalAccessCredential)
        .filter(
            UniversalAccessCredential.member_id == caller.id,
            UniversalAccessCredential.enabled.is_(True),
            UniversalAccessCredential.key_type == UniversalAccessKeyType.PRIVATE.value,
        )
        .order_by(UniversalAccessCredential.created_at.desc())
        .all()
    )
    return [
        {
            "credential_id": row.id,
            "code": row.code,
            "key_type": UniversalAccessKeyType.PRIVATE.value,
            "zone_ids": _int_ids(row.zone_ids),
            "zones": [_zone_summary(zone) for zone in _zones_by_ids(db, _int_ids(row.zone_ids))],
            "enabled": bool(row.enabled),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def search_public_keys(db: Session, *, caller: Owner, query: str, limit: int = 10) -> list[dict[str, Any]]:
    del caller
    needle = " ".join(str(query or "").split()).upper()
    if len(needle) < 3:
        return []
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    rows = (
        db.query(UniversalAccessCredential)
        .filter(
            UniversalAccessCredential.enabled.is_(True),
            UniversalAccessCredential.key_type == UniversalAccessKeyType.PUBLIC.value,
            func.upper(UniversalAccessCredential.code).like(like, escape="\\"),
        )
        .order_by(UniversalAccessCredential.code.asc())
        .limit(max(1, min(int(limit), 10)))
        .all()
    )
    return [
        {
            "credential_id": row.id,
            "code": row.code,
            "key_type": UniversalAccessKeyType.PUBLIC.value,
            "zone_ids": _int_ids(row.zone_ids),
            "zones": [_zone_summary(zone) for zone in _zones_by_ids(db, _int_ids(row.zone_ids))],
        }
        for row in rows
    ]


def activate_public_key(db: Session, *, caller: Owner, code: str) -> dict[str, Any]:
    cleaned = _clean(code, limit=32).upper()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a public access key.",
        )
    credential = (
        db.query(UniversalAccessCredential)
        .filter(func.upper(UniversalAccessCredential.code) == cleaned)
        .first()
    )
    if credential is None or not credential.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Access key not found.")
    if _credential_key_type(credential) != UniversalAccessKeyType.PUBLIC.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only public access keys can be entered here.",
        )
    existing = (
        db.query(UniversalAccessActivation)
        .filter(
            UniversalAccessActivation.credential_id == credential.id,
            UniversalAccessActivation.owner_id == caller.id,
        )
        .first()
    )
    if existing is None:
        db.add(
            UniversalAccessActivation(
                credential_id=credential.id,
                owner_id=caller.id,
            )
        )
        db.commit()
    return {
        "credential_id": credential.id,
        "code": credential.code,
        "key_type": UniversalAccessKeyType.PUBLIC.value,
        "zone_ids": _int_ids(credential.zone_ids),
        "zones": [_zone_summary(zone) for zone in _zones_by_ids(db, _int_ids(credential.zone_ids))],
    }


def list_activated_keys(db: Session, *, caller: Owner) -> list[dict[str, Any]]:
    rows = (
        db.query(UniversalAccessActivation, UniversalAccessCredential)
        .join(
            UniversalAccessCredential,
            UniversalAccessCredential.id == UniversalAccessActivation.credential_id,
        )
        .filter(
            UniversalAccessActivation.owner_id == caller.id,
            UniversalAccessCredential.enabled.is_(True),
        )
        .order_by(UniversalAccessActivation.created_at.desc())
        .all()
    )
    return [
        {
            "credential_id": credential.id,
            "code": credential.code,
            "key_type": _credential_key_type(credential),
            "zone_ids": _int_ids(credential.zone_ids),
            "zones": [_zone_summary(zone) for zone in _zones_by_ids(db, _int_ids(credential.zone_ids))],
            "activated_at": activation.created_at.isoformat() if activation.created_at else None,
        }
        for activation, credential in rows
    ]


def enabled_credentials_for(db: Session, owner: Owner) -> list[UniversalAccessCredential]:
    private_rows = (
        db.query(UniversalAccessCredential)
        .filter(
            UniversalAccessCredential.member_id == owner.id,
            UniversalAccessCredential.enabled.is_(True),
            or_(
                UniversalAccessCredential.key_type == UniversalAccessKeyType.PRIVATE.value,
                UniversalAccessCredential.key_type.is_(None),
            ),
        )
        .all()
    )
    activated_ids = [
        int(row.credential_id)
        for row in db.query(UniversalAccessActivation)
        .filter(UniversalAccessActivation.owner_id == owner.id)
        .all()
    ]
    public_rows: list[UniversalAccessCredential] = []
    if activated_ids:
        public_rows = (
            db.query(UniversalAccessCredential)
            .filter(
                UniversalAccessCredential.id.in_(tuple(activated_ids)),
                UniversalAccessCredential.enabled.is_(True),
                UniversalAccessCredential.key_type == UniversalAccessKeyType.PUBLIC.value,
            )
            .all()
        )
    by_id = {int(row.id): row for row in [*private_rows, *public_rows]}
    return list(by_id.values())


def universal_primary_zones(
    db: Session,
    owner: Owner,
    *,
    already_ids: set[int],
) -> list[Zone]:
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
    try:
        from app.crud.zone import list_zones_with_geojson_by_ids

        rows = list_zones_with_geojson_by_ids(db, wanted, active_only=True)
    except Exception:
        rows = (
            db.query(Zone)
            .options(defer(Zone.geo_fence_polygon))
            .filter(Zone.id.in_(tuple(wanted)), Zone.active.is_(True))
            .all()
        )
        by_id = {int(row.id): row for row in rows}
        rows = [by_id[zone_id] for zone_id in wanted if zone_id in by_id]
    matched: list[Zone] = []
    for zone in rows:
        if not _zone_is_shareable(zone):
            continue
        setattr(zone, "shared_via_universal", True)
        matched.append(zone)
    return matched


# Backward-compatible aliases.
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
    del photo_id, business_document
    return create_invite_request(
        db,
        admin=admin,
        invitee_email=invitee_email,
        zone_ids=zone_ids,
        note=note,
        key_type="private",
    )


def place_candidate_zones(
    db: Session,
    *,
    country: str,
    region: str,
    city: str,
) -> list[dict[str, Any]]:
    """Legacy helper retained for older tests; new flow uses explicit zone selection."""
    del db, country, region, city
    return []
