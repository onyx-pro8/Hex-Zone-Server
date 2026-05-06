"""Approved-guest APIs: peers, zone messages (PERMISSION/CHAT), delivery blocks."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, defer

from app.domain.message_types import (
    CanonicalMessageType,
    MessageScope,
    normalize_message_type,
    type_category,
    type_scope,
)
from app.models import MessageBlock, Owner, Zone, ZoneMessageEvent
from app.services.access_policy import zone_listing_owner_ids
from app.models.owner import OwnerRole
from app.services import guest_access_service
from app.websocket.manager import ws_manager

logger = logging.getLogger(__name__)

GUEST_VISIBLE_TYPES = frozenset(
    {
        CanonicalMessageType.CHAT.value,
        CanonicalMessageType.PERMISSION.value,
    }
)
GUEST_WRITABLE_TYPES = frozenset({CanonicalMessageType.CHAT.value})

# Cap how many newest zone rows we scan when assembling a guest access thread without
# brittle JSON-SQL (PERMISSION rows only carry guest_id inside body_json).
_MEMBER_GUEST_THREAD_MAX_SCAN = 5000


def _guest_messaging_peer_owner_ids(db: Session, *, zone_id: str) -> set[int]:
    """Staff a guest may address with **CHAT**: zone **ADMINISTRATOR** owners + **`zones.owner_id`** rows.

    Also includes **`resolve_primary_zone_admin_owner`** so a primary admin appears when data is split
    across **`owners`** vs **`zones`** (without granting every **`USER`** in the same **`zone_id`** string).
    """

    zid = zone_id.strip()
    if not zid:
        return set()
    admin_ids = {
        row[0]
        for row in (
            db.query(Owner.id)
            .filter(
                Owner.zone_id == zid,
                Owner.role == OwnerRole.ADMINISTRATOR,
                Owner.active.is_(True),
            )
            .all()
        )
    }
    host_ids = {
        row[0]
        for row in (
            db.query(Zone.owner_id)
            .filter(Zone.zone_id == zid, Zone.active.is_(True))
            .distinct()
            .all()
        )
    }
    out = admin_ids | host_ids
    primary = guest_access_service.resolve_primary_zone_admin_owner(db, zid)
    if primary and primary.active:
        out.add(primary.id)
    return out


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
    staff_ids = _guest_messaging_peer_owner_ids(db, zone_id=zone_id)
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


def belongs_to_guest_access_thread(row: ZoneMessageEvent, guest_id: str) -> bool:
    """True when this zone event belongs to **guest_id** access thread (CHAT/PERMISSION)."""
    gid = (guest_id or "").strip()
    if not gid:
        return False
    if row.type not in GUEST_VISIBLE_TYPES:
        return False
    if row.sender_guest_id == gid:
        return True
    return _body_guest_id_matches(row, gid)


def list_guest_access_thread_for_zone_member(
    db: Session,
    *,
    zone_id: str,
    guest_id: str,
    peer_owner_id: int | None,
    skip: int,
    limit: int,
) -> list[ZoneMessageEvent]:
    """Return **`ZoneMessageEvent`** rows visible to admins for the guest access thread.

    Uses an in-memory filter so **PERMISSION** rows ( **`sender_id`/ `sender_guest_id` null**, guest only in **`body`**)
    match reliably on SQLite **and** PostgreSQL (avoids dialect-specific JSON path SQL).
    """
    zid = zone_id.strip()
    gid = (guest_id or "").strip()
    sk = max(0, int(skip))
    lim = max(1, min(int(limit), 1000))

    rows = (
        db.query(ZoneMessageEvent)
        .filter(
            ZoneMessageEvent.zone_id == zid,
            ZoneMessageEvent.type.in_(tuple(GUEST_VISIBLE_TYPES)),
        )
        .order_by(ZoneMessageEvent.created_at.desc())
        .limit(_MEMBER_GUEST_THREAD_MAX_SCAN)
        .all()
    )

    peer = peer_owner_id
    filtered: list[ZoneMessageEvent] = []
    for row in rows:
        if not belongs_to_guest_access_thread(row, gid):
            continue
        if peer is not None and not _guest_thread_row_matches_peer(row, peer, gid):
            continue
        filtered.append(row)
    return filtered[sk : sk + lim]


def _guest_thread_row_matches_peer(row: ZoneMessageEvent, peer: int, viewer_guest_id: str) -> bool:
    """Narrow admin/member guest-thread view to one staff peer while keeping PERMISSION audit lines."""
    if row.type == CanonicalMessageType.PERMISSION.value:
        return True
    if row.sender_guest_id == viewer_guest_id and row.receiver_id == peer:
        return True
    if row.sender_id == peer and _body_guest_id_matches(row, viewer_guest_id):
        return True
    return False


def manageable_zone_ids_for_permission_inbox(db: Session, owner: Owner) -> set[str]:
    """Zone ids that may surface **PERMISSION** rows in **`GET /messages`** for this owner."""

    ids: set[str] = set()
    z0 = (owner.zone_id or "").strip()
    if z0:
        ids.add(z0)
    allowed = zone_listing_owner_ids(db, owner)
    if not allowed:
        return ids
    for (zid,) in db.query(Zone.zone_id).filter(Zone.owner_id.in_(allowed), Zone.active.is_(True)).distinct().all():
        if zid:
            ids.add(zid.strip())
    return ids


def list_zone_permission_events_for_owner_feed(
    db: Session,
    *,
    owner: Owner,
    max_scan: int = 3000,
) -> list[ZoneMessageEvent]:
    """Recent **`PERMISSION`** zone events for zones this owner may administer (guest access feed)."""

    zonable = manageable_zone_ids_for_permission_inbox(db, owner)
    if not zonable:
        return []
    chunk = tuple(sorted(zonable))
    rows = (
        db.query(ZoneMessageEvent)
        .filter(
            ZoneMessageEvent.zone_id.in_(chunk),
            ZoneMessageEvent.type == CanonicalMessageType.PERMISSION.value,
        )
        .order_by(ZoneMessageEvent.created_at.desc())
        .limit(max(1, min(int(max_scan), 8000)))
        .all()
    )
    visible: list[ZoneMessageEvent] = []
    for row in rows:
        if guest_access_service.can_manage_zone_guest_requests(db, owner, row.zone_id):
            visible.append(row)
    return visible


def zone_message_event_to_member_zone_message_response(row: ZoneMessageEvent):
    """Build **`ZoneMessageResponse`** for admin/member clients (import local to avoid circular imports)."""
    from app.schemas.schemas import MessageVisibilityEnum, ZoneMessageResponse

    gtype = normalize_message_type(row.type)
    return ZoneMessageResponse(
        id=row.id,
        zone_id=row.zone_id,
        sender_id=row.sender_id,
        receiver_id=row.receiver_id,
        type=row.type,
        category=type_category(gtype).value,
        scope=type_scope(gtype).value,
        visibility=(
            MessageVisibilityEnum.PRIVATE
            if type_scope(gtype) == MessageScope.PRIVATE
            else MessageVisibilityEnum.PUBLIC
        ),
        message=row.text,
        created_at=row.created_at,
    )


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
    """List guest-visible thread rows (PERMISSION + CHAT) with stable peer semantics.

    PERMISSION audit lines for a guest are visible in every staff-peer thread for that guest
    (same behavior as **GET /messages** + **guest_id** for members). Implements cursor paging
    over a capped recent scan so SQLite/Postgres JSON differences do not drop rows.
    """
    lim = max(1, min(int(limit), 200))
    zid = zone_id.strip()
    gid = (guest_id or "").strip()
    peer = with_owner_id

    rows = (
        db.query(ZoneMessageEvent)
        .filter(ZoneMessageEvent.zone_id == zid)
        .filter(ZoneMessageEvent.type.in_(tuple(GUEST_VISIBLE_TYPES)))
        .order_by(ZoneMessageEvent.created_at.desc(), ZoneMessageEvent.id.desc())
        .limit(_MEMBER_GUEST_THREAD_MAX_SCAN)
        .all()
    )

    filtered: list[ZoneMessageEvent] = []
    for row in rows:
        if not belongs_to_guest_access_thread(row, gid):
            continue
        if peer is not None and not _guest_thread_row_matches_peer(row, peer, gid):
            continue
        if before_created_at is not None and before_id:
            if row.created_at > before_created_at or (
                row.created_at == before_created_at and row.id >= before_id
            ):
                continue
        filtered.append(row)

    page = filtered[: lim + 1]
    next_cursor = None
    if len(page) > lim:
        tail = page[lim]
        next_cursor = f"{tail.created_at.isoformat()}|{tail.id}"
        page = page[:lim]
    return page, next_cursor


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
    receiver = db.query(Owner).filter(Owner.id == to_owner_id, Owner.active.is_(True)).first()
    if not receiver:
        return None
    if to_owner_id not in _guest_messaging_peer_owner_ids(db, zone_id=zid):
        return {"__reject__": "forbidden", "message": "Recipient is not an authorized host/admin peer for this zone."}

    if msg_type not in GUEST_WRITABLE_TYPES:
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

    try:
        canonical = normalize_message_type(msg_type)
    except ValueError:
        return {"__reject__": "invalid_type", "message": "Unsupported message type."}

    if canonical == CanonicalMessageType.PERMISSION:
        return {
            "__reject__": "permission_manual_disabled",
            "message": "PERMISSION messages are server-generated only for guest workflow transitions.",
        }
    if canonical.value not in GUEST_WRITABLE_TYPES:
        return {"__reject__": "invalid_type", "message": "Only CHAT is allowed for guest threads."}

    # Local import guards against partial module initialization edge cases.
    from app.services import guest_access_service as _guest_access_service

    if not _guest_access_service.can_manage_zone_guest_requests(db, sender, zid):
        return {"__reject__": "forbidden", "message": "You cannot message guests for this zone."}

    row = _guest_access_service.get_guest_access_session_by_guest_id(db, gid)
    if not row or row.zone_id != zid:
        return {"__reject__": "not_found", "message": "Guest session not found for this zone."}

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

    cells: list[str] = []
    zoom_level = 14
    center: dict[str, float] | None = None
    geojson_fc: dict[str, Any] | None = None
    bounds: dict[str, float] | None = None
    if z:
        hc = getattr(z, "h3_cells", None) or []
        if isinstance(hc, list):
            cells = [str(c) for c in hc if str(c)]
        elif isinstance(hc, str):
            cells = [hc]
        params = z.parameters if isinstance(z.parameters, dict) else {}
        guest_map = params.get("guest_map") if isinstance(params.get("guest_map"), dict) else {}
        mz = guest_map.get("zoom")
        if isinstance(mz, (int, float)) and mz > 0:
            zoom_level = float(mz)
        cen = guest_map.get("center") if isinstance(guest_map.get("center"), dict) else {}
        lat, lng = cen.get("lat"), cen.get("lng")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            center = {"lat": float(lat), "lng": float(lng)}
        gj = guest_map.get("geojson")
        if isinstance(gj, dict) and gj.get("type") == "FeatureCollection":
            geojson_fc = gj
        b = guest_map.get("bounds") if isinstance(guest_map.get("bounds"), dict) else {}
        s, n, ew, ww = (
            b.get("south"),
            b.get("north"),
            b.get("east"),
            b.get("west"),
        )
        if all(isinstance(v, (int, float)) for v in (s, n, ew, ww)):
            bounds = {
                "south": float(s),
                "north": float(n),
                "east": float(ew),
                "west": float(ww),
            }

    map_payload = {
        "center": center,
        "zoom": zoom_level,
        "cells": cells,
        **({"bounds": bounds} if bounds else {}),
        **({"geojson": geojson_fc} if geojson_fc else {}),
    }

    return {
        "zone_id": zone_id.strip(),
        "label": label,
        "welcome_text": welcome,
        "links": [],
        "cells": cells,
        "map": map_payload,
    }
