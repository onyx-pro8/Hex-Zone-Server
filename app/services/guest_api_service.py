"""Approved-guest APIs: peers, zone messages (PERMISSION/CHAT), delivery blocks."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, defer

from app.domain.message_types import CanonicalMessageType, normalize_message_type, type_category, type_scope
from app.models import MessageBlock, Owner, Zone, ZoneMessageEvent
from app.models.owner import OwnerRole
from app.services import guest_access_service
from app.websocket.manager import ws_manager

logger = logging.getLogger(__name__)

GUEST_ALLOWED_TYPES = frozenset({CanonicalMessageType.PERMISSION.value, CanonicalMessageType.CHAT.value})


def guest_type_blocked(db: Session, recipient_owner_id: int, message_type: str) -> bool:
    """True if recipient recorded a block on this message type (applies to any sender, including guests)."""
    return (
        db.query(MessageBlock)
        .filter(
            MessageBlock.owner_id == recipient_owner_id,
            MessageBlock.blocked_message_type == message_type,
        )
        .first()
        is not None
    )


def list_zone_peers_for_guest(db: Session, *, zone_id: str) -> list[dict]:
    staff_ids = guest_access_service.zone_staff_owner_ids(db, zone_id)
    if not staff_ids:
        return []
    owners = (
        db.query(Owner)
        .filter(Owner.id.in_(staff_ids), Owner.active.is_(True))
        .order_by(Owner.id.asc())
        .all()
    )
    out: list[dict] = []
    for o in owners:
        role = o.role.value if isinstance(o.role, OwnerRole) else str(o.role)
        display = f"{o.first_name} {o.last_name}".strip() or o.email
        can_chat = not guest_type_blocked(db, o.id, CanonicalMessageType.CHAT.value)
        out.append(
            {
                "peer_kind": "owner",
                "owner_id": o.id,
                "display_name": display,
                "role": role,
                "can_receive_chat": can_chat,
            }
        )
    return out


def _body_guest_id_matches(row: ZoneMessageEvent, guest_id: str) -> bool:
    body = row.body_json or {}
    if not isinstance(body, dict):
        return False
    gid = str(body.get("guest_id") or "").strip()
    return gid == guest_id


def _guest_visible_message_predicate(guest_id: str, with_owner_id: int | None):
    body_guest = ZoneMessageEvent.body_json["guest_id"].as_string() == guest_id
    parts = [
        ZoneMessageEvent.sender_guest_id == guest_id,
        body_guest,
    ]
    base = or_(*parts)
    if with_owner_id is None:
        return base
    peer = with_owner_id
    thread = or_(
        and_(ZoneMessageEvent.sender_guest_id == guest_id, ZoneMessageEvent.receiver_id == peer),
        and_(ZoneMessageEvent.sender_id == peer, body_guest),
    )
    return and_(base, thread)


def list_guest_zone_messages(
    db: Session,
    *,
    guest_id: str,
    zone_id: str,
    with_owner_id: int | None,
    limit: int,
    before_id: str | None,
    before_created_at: datetime | None,
) -> tuple[list[ZoneMessageEvent], str | None]:
    lim = max(1, min(int(limit), 200))
    q = (
        db.query(ZoneMessageEvent)
        .filter(ZoneMessageEvent.zone_id == zone_id.strip())
        .filter(ZoneMessageEvent.type.in_(tuple(GUEST_ALLOWED_TYPES)))
        .filter(_guest_visible_message_predicate(guest_id, with_owner_id))
    )
    if before_created_at is not None and before_id:
        q = q.filter(
            or_(
                ZoneMessageEvent.created_at < before_created_at,
                and_(ZoneMessageEvent.created_at == before_created_at, ZoneMessageEvent.id < before_id),
            )
        )
    rows = q.order_by(ZoneMessageEvent.created_at.desc(), ZoneMessageEvent.id.desc()).limit(lim + 1).all()
    next_cursor = None
    if len(rows) > lim:
        tail = rows[lim]
        next_cursor = f"{tail.created_at.isoformat()}|{tail.id}"
        rows = rows[:lim]
    return rows, next_cursor


def _serialize_from(row: ZoneMessageEvent, viewer_guest_id: str) -> dict[str, Any]:
    if row.sender_guest_id:
        return {"kind": "guest", "guest_id": row.sender_guest_id, "owner_id": None}
    if row.sender_id is not None:
        return {"kind": "owner", "guest_id": None, "owner_id": row.sender_id}
    if _body_guest_id_matches(row, viewer_guest_id):
        return {"kind": "guest", "guest_id": viewer_guest_id, "owner_id": None}
    return {"kind": "guest", "guest_id": None, "owner_id": None}


def _serialize_to(row: ZoneMessageEvent, viewer_guest_id: str) -> dict[str, Any]:
    if row.receiver_id is not None:
        return {"kind": "owner", "guest_id": None, "owner_id": row.receiver_id}
    if row.type == CanonicalMessageType.PERMISSION.value and row.sender_id is None:
        return {"kind": "zone_broadcast", "guest_id": None, "owner_id": None}
    if _body_guest_id_matches(row, viewer_guest_id):
        return {"kind": "guest", "guest_id": viewer_guest_id, "owner_id": None}
    return {"kind": "zone_broadcast", "guest_id": None, "owner_id": None}


def serialize_zone_message_for_guest(row: ZoneMessageEvent, viewer_guest_id: str) -> dict[str, Any]:
    created = row.created_at.replace(microsecond=0).isoformat() + "Z"
    return {
        "id": row.id,
        "zone_id": row.zone_id,
        "type": row.type,
        "created_at": created,
        "text": row.text,
        "from": _serialize_from(row, viewer_guest_id),
        "to": _serialize_to(row, viewer_guest_id),
        "raw_payload": dict(row.body_json) if isinstance(row.body_json, dict) else {},
    }


def parse_message_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if not cursor or "|" not in cursor:
        return None, None
    ts_s, mid = cursor.split("|", 1)
    try:
        ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None, None
    return ts, mid


def create_guest_zone_message(
    db: Session,
    *,
    guest_id: str,
    guest_display_name: str,
    zone_id: str,
    msg_type: str,
    text: str | None,
    to_owner_id: int,
    msg: dict | None,
) -> dict[str, Any] | None:
    """Returns serialized message dict, or None if should reject with HTTP (caller maps)."""
    zid = zone_id.strip()
    receiver = db.query(Owner).filter(Owner.id == to_owner_id, Owner.zone_id == zid, Owner.active.is_(True)).first()
    if not receiver:
        return None

    if msg_type not in GUEST_ALLOWED_TYPES:
        return {"__reject__": "forbidden_message_type"}

    if guest_type_blocked(db, receiver.id, msg_type):
        return {"__reject__": "blocked"}

    try:
        canonical = normalize_message_type(msg_type)
    except ValueError:
        return {"__reject__": "forbidden_message_type"}

    body: dict[str, Any] = {"guest_id": guest_id, "guest_name": guest_display_name}
    if msg:
        body.update(msg)
    if canonical == CanonicalMessageType.CHAT:
        display_text = (text or "").strip()
        if not display_text:
            return {"__reject__": "validation"}
    else:
        display_text = (text or "").strip() or (body.get("text") if isinstance(body.get("text"), str) else "") or ""

    event = ZoneMessageEvent(
        zone_id=zid,
        sender_id=None,
        sender_guest_id=guest_id,
        receiver_id=to_owner_id,
        type=canonical.value,
        category=type_category(canonical),
        scope=type_scope(canonical),
        text=display_text or "(no text)",
        body_json=body,
        metadata_json={"flow": "guest_api", "guest_id": guest_id},
    )
    db.add(event)
    db.flush()
    db.refresh(event)
    return serialize_zone_message_for_guest(event, guest_id)


async def notify_guest_message_recipient(*, recipient_owner_id: int, payload: dict) -> None:
    await ws_manager.broadcast_to_users([recipient_owner_id], "guest_zone_message", payload)


def create_member_to_guest_zone_message(
    db: Session,
    *,
    sender: Owner,
    zone_id: str,
    guest_id: str,
    text: str,
    msg_type: str,
) -> ZoneMessageEvent | dict[str, Any]:
    """Persist **ZoneMessageEvent** from a member to a guest thread (same store as **GET /api/guest/messages**).

    Returns the persisted **`ZoneMessageEvent`**, or **`{"__reject__": "<code>", "message": "..."}`** on validation
    or policy failure (caller maps to HTTP).
    """
    zid = zone_id.strip()
    gid = (guest_id or "").strip()
    if not zid or not gid:
        return {"__reject__": "validation", "message": "zone_id and guest_id are required."}

    if not guest_access_service.can_manage_zone_guest_requests(db, sender, zid):
        return {"__reject__": "forbidden", "message": "You cannot message guests for this zone."}

    row = guest_access_service.get_guest_access_session_by_guest_id(db, gid)
    if not row or row.zone_id != zid:
        return {"__reject__": "not_found", "message": "Guest session not found for this zone."}

    try:
        canonical = normalize_message_type(msg_type)
    except ValueError:
        return {"__reject__": "invalid_type", "message": "Unsupported message type."}

    if canonical.value not in GUEST_ALLOWED_TYPES:
        return {"__reject__": "invalid_type", "message": "Only PERMISSION and CHAT are allowed for guest threads."}

    display_text = (text or "").strip()
    if canonical == CanonicalMessageType.CHAT and not display_text:
        return {"__reject__": "validation", "message": "message text is required for CHAT."}

    body: dict[str, Any] = {
        "guest_id": gid,
        "guest_name": row.guest_name,
        "zone_id": zid,
    }
    event = ZoneMessageEvent(
        zone_id=zid,
        sender_id=sender.id,
        sender_guest_id=None,
        receiver_id=None,
        type=canonical.value,
        category=type_category(canonical),
        scope=type_scope(canonical),
        text=display_text or "(no text)",
        body_json=body,
        metadata_json={"flow": "member_to_guest", "guest_id": gid},
    )
    db.add(event)
    db.flush()
    db.refresh(event)
    return event


def get_guest_dashboard_safe(db: Session, *, zone_id: str) -> dict[str, Any]:
    z = (
        db.query(Zone)
        .options(defer(Zone.geo_fence_polygon))
        .filter(Zone.zone_id == zone_id.strip(), Zone.active.is_(True))
        .order_by(Zone.id.asc())
        .first()
    )
    label = z.name if z else zone_id
    welcome = "Welcome to the zone guest dashboard."
    return {
        "zone_id": zone_id.strip(),
        "label": label,
        "welcome_text": welcome,
        "links": [],
    }
