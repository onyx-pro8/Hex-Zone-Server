"""Approved-guest APIs: peers, zone messages (PERMISSION/CHAT), delivery blocks."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.domain.message_types import (
    CanonicalMessageType,
    MessageScope,
    normalize_message_type,
    type_category,
    type_scope,
)
from app.domain.permission_visibility import PERMISSION_VISIBILITY_ZONE_PENDING_BROADCAST
from app.models import GuestAccessSession, Owner, Zone, ZoneMessageEvent
from app.services import message_block_service
from app.services.access_policy import zone_listing_owner_ids
from app.models.owner import OwnerRole
from app.services import guest_access_service
from app.core.config import settings
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
    """True if recipient blocks this delivery (type-only rules apply for guest senders)."""
    return message_block_service.is_delivery_blocked(
        db,
        recipient_owner_id=recipient_owner_id,
        sender_owner_id=None,
        message_type=message_type,
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


def access_thread_guest_marker(row: ZoneMessageEvent) -> str | None:
    """Infer Access-thread guest UUID from **`ZoneMessageEvent`** (guest-send or staff→guest)."""
    if row.sender_guest_id:
        s = row.sender_guest_id.strip()
        return s or None
    body = row.body_json if isinstance(row.body_json, dict) else {}
    g = str(body.get("guest_id") or "").strip()
    if g:
        return g
    meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    g = str(meta.get("guest_id") or "").strip()
    return g or None


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


def merged_inbox_permission_event_visible(db: Session, viewer: Owner, row: ZoneMessageEvent) -> bool:
    """Whether **`row`** (**`PERMISSION`**) may appear in **`GET /messages`** merged inbox for **`viewer`**."""

    if row.type != CanonicalMessageType.PERMISSION.value:
        return True
    zid = (row.zone_id or "").strip()
    if not zid:
        return False
    meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    vis = meta.get("permission_visibility")
    if vis == PERMISSION_VISIBILITY_ZONE_PENDING_BROADCAST:
        if row.guest_access_session_id is None:
            return False
        sess = db.get(GuestAccessSession, row.guest_access_session_id)
        pending_unexpected = (
            sess is not None
            and sess.kind == "unexpected"
            and (sess.resolution or "") == "pending"
        )
        if pending_unexpected and guest_access_service.can_manage_zone_guest_requests(db, viewer, zid):
            return True
        return viewer.id == row.sender_id or viewer.id == row.receiver_id
    if row.receiver_id is None:
        # Legacy PERMISSION rows (no `receiver_id`): keep the historical merged-inbox cohort so
        # production DBs are not blank until backfill. New server-written rows always set `receiver_id`.
        return guest_access_service.can_manage_zone_guest_requests(db, viewer, zid)
    return viewer.id == row.sender_id or viewer.id == row.receiver_id


def list_guest_access_thread_for_zone_member(
    db: Session,
    *,
    zone_id: str,
    guest_id: str,
    peer_owner_id: int | None,
    skip: int,
    limit: int,
    viewer_owner_id: int,
) -> list[ZoneMessageEvent]:
    """Return **`ZoneMessageEvent`** rows visible to admins for the guest access thread.

    Uses an in-memory filter so **PERMISSION** rows (guest id in **`body`** / **`metadata`**) match reliably on SQLite
    **and** PostgreSQL. **`viewer_owner_id`** scopes **PERMISSION** rows to the same visibility rules as the merged
    member inbox (direct vs **`zone_pending_broadcast`**).
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

    viewer = db.get(Owner, viewer_owner_id)
    if not viewer:
        return []

    peer = peer_owner_id
    filtered: list[ZoneMessageEvent] = []
    for row in rows:
        if not belongs_to_guest_access_thread(row, gid):
            continue
        if peer is not None and not _guest_thread_row_matches_peer(row, peer, gid):
            continue
        if row.type == CanonicalMessageType.PERMISSION.value and not merged_inbox_permission_event_visible(
            db, viewer, row,
        ):
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
        if merged_inbox_permission_event_visible(db, owner, row):
            visible.append(row)
    return visible


_GEO_INBOX_TYPES: frozenset[str] = frozenset(
    {
        CanonicalMessageType.UNKNOWN.value,
        CanonicalMessageType.PANIC.value,
        CanonicalMessageType.NS_PANIC.value,
        CanonicalMessageType.SENSOR.value,
        CanonicalMessageType.SERVICE.value,
        CanonicalMessageType.WELLNESS_CHECK.value,
        CanonicalMessageType.PA.value,
        CanonicalMessageType.PRIVATE.value,
    }
)


def list_geo_propagation_events_for_owner_inbox(
    db: Session,
    *,
    owner: Owner,
    max_scan: int = 3000,
) -> list[ZoneMessageEvent]:
    """Geo propagation rows where this owner is a delivered recipient.

    Lets users 44/45/etc. see UNKNOWN / PANIC / SENSOR / etc. on **`GET /messages`**
    even when they were offline at fan-out time. Visibility is driven by
    **`metadata.delivered_owner_ids`** written by the propagation service so the
    same recipient list used for WebSocket / push also drives the inbox.
    """
    rows = (
        db.query(ZoneMessageEvent)
        .filter(ZoneMessageEvent.type.in_(tuple(_GEO_INBOX_TYPES)))
        .order_by(ZoneMessageEvent.created_at.desc())
        .limit(max(1, min(int(max_scan), 8000)))
        .all()
    )
    visible: list[ZoneMessageEvent] = []
    for row in rows:
        if row.sender_id == owner.id:
            visible.append(row)
            continue
        if row.receiver_id == owner.id:
            visible.append(row)
            continue
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        delivered = meta.get("delivered_owner_ids")
        if isinstance(delivered, list) and owner.id in delivered:
            visible.append(row)
    return visible


def list_zone_guest_access_chat_events_for_owner_inbox(
    db: Session,
    *,
    owner: Owner,
    max_scan: int = 3000,
) -> list[ZoneMessageEvent]:
    """**CHAT** **`ZoneMessageEvent`** rows surfaced on **`GET /messages`** for this owner.

    Visibility (peer-style, analogous to **`with_owner_id`** on **`GET /api/guest/messages`**):

    - **Guest → staff**: include when **`receiver_id`** is this owner **`and`** **`sender_guest_id`** is set
      (true Access guest channel message to this peer).

    - **Staff → guest**: include when **`sender_id`** is this owner **`and`** a guest id marker exists on the row
      (**`body`** or **`metadata` **`guest_id`**), matching member→guest **CHAT** persistence.

    Additionally requires **`guest_access_service.can_manage_zone_guest_requests`** for the row **`zone_id`**
    (same gate as merged **PERMISSION** audit lines).
    """
    zonable = manageable_zone_ids_for_permission_inbox(db, owner)
    if not zonable:
        return []
    chunk = tuple(sorted(zonable))
    rows = (
        db.query(ZoneMessageEvent)
        .filter(
            ZoneMessageEvent.zone_id.in_(chunk),
            ZoneMessageEvent.type == CanonicalMessageType.CHAT.value,
        )
        .order_by(ZoneMessageEvent.created_at.desc())
        .limit(max(1, min(int(max_scan), 8000)))
        .all()
    )
    visible: list[ZoneMessageEvent] = []
    for row in rows:
        if not guest_access_service.can_manage_zone_guest_requests(db, owner, row.zone_id):
            continue
        if row.receiver_id == owner.id and row.sender_guest_id:
            visible.append(row)
            continue
        if row.sender_id == owner.id and access_thread_guest_marker(row):
            visible.append(row)
    return visible


def zone_message_event_to_member_zone_message_response(
    row: ZoneMessageEvent,
    db: Session | None = None,
):
    """Build **`ZoneMessageResponse`** for admin/member clients (import local to avoid circular imports)."""
    from app.schemas.schemas import MessageVisibilityEnum, ZoneMessageResponse

    gtype = normalize_message_type(row.type)
    meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    raw_vis = meta.get("permission_visibility")
    perm_vis = raw_vis if raw_vis in ("direct", "zone_pending_broadcast") else None
    if row.type != CanonicalMessageType.PERMISSION.value:
        perm_vis = None
    session_pending: bool | None = None
    if (
        db is not None
        and row.guest_access_session_id is not None
        and perm_vis == PERMISSION_VISIBILITY_ZONE_PENDING_BROADCAST
    ):
        sess = db.get(GuestAccessSession, row.guest_access_session_id)
        if sess is not None:
            session_pending = bool(sess.kind == "unexpected" and (sess.resolution or "") == "pending")

    # Sender display name: prefer a broadcast name embedded in the event payload
    # (set by clients), else the sender owner's broadcast name / first+last.
    broadcast_name = None
    embedded = meta.get("broadcast_name") or meta.get("broadcastName")
    if isinstance(embedded, str) and embedded.strip():
        broadcast_name = embedded.strip()
    elif db is not None and row.sender_id is not None:
        from app.models import Owner

        sender_owner = db.get(Owner, row.sender_id)
        if sender_owner is not None:
            broadcast_name = sender_owner.message_display_name

    latitude = None
    longitude = None
    for source in (meta, meta.get("position") if isinstance(meta.get("position"), dict) else None):
        if not isinstance(source, dict):
            continue
        lat = source.get("latitude")
        lng = source.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            latitude = float(lat)
            longitude = float(lng)
            break

    return ZoneMessageResponse(
        id=row.id,
        zone_id=row.zone_id,
        sender_id=row.sender_id,
        receiver_id=row.receiver_id,
        broadcast_name=broadcast_name,
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
        latitude=latitude,
        longitude=longitude,
        guest_id=access_thread_guest_marker(row),
        permission_visibility=perm_vis,
        guest_access_session_id=row.guest_access_session_id,
        session_pending=session_pending,
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
    meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    raw_payload = dict(row.body_json) if isinstance(row.body_json, dict) else {}
    pv = meta.get("permission_visibility")
    if pv in ("direct", "zone_pending_broadcast"):
        raw_payload = {**raw_payload, "permission_visibility": pv}
    return {
        "id": row.id,
        "zone_id": row.zone_id,
        "type": row.type,
        "created_at": created,
        "text": row.text,
        "from": _serialize_from(row, viewer_guest_id),
        "to": _serialize_to(row, viewer_guest_id),
        "raw_payload": raw_payload,
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


async def notify_access_chat_inbox_ws(db: Session, row: ZoneMessageEvent) -> None:
    """Push **`NEW_MESSAGE`** to staff parties with the same **`ZoneMessageResponse`** shape as **`GET /messages`**."""

    if not settings.MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT:
        return

    recipients: set[int] = set()
    if row.receiver_id is not None:
        recipients.add(int(row.receiver_id))
    if row.sender_id is not None:
        recipients.add(int(row.sender_id))
    if not recipients:
        return

    payload = zone_message_event_to_member_zone_message_response(row).model_dump(mode="json")
    msg_type = str(row.type or "")
    deliver_to = [
        uid
        for uid in recipients
        if not message_block_service.is_delivery_blocked(
            db,
            recipient_owner_id=uid,
            sender_owner_id=row.sender_id,
            message_type=msg_type,
        )
    ]
    if not deliver_to:
        return
    await ws_manager.broadcast_to_users(sorted(deliver_to), "NEW_MESSAGE", payload)


def create_member_to_guest_zone_message(
    db: Session,
    *,
    sender: Owner,
    zone_id: str,
    guest_id: str,
    text: str,
    msg_type: str,
    latitude: float | None = None,
    longitude: float | None = None,
    msg: dict[str, Any] | None = None,
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
    metadata: dict[str, Any] = {"flow": "member_to_guest", "guest_id": gid}
    if isinstance(msg, dict):
        metadata.update(msg)
    if latitude is not None and longitude is not None:
        metadata["latitude"] = latitude
        metadata["longitude"] = longitude
        metadata["position"] = {"latitude": latitude, "longitude": longitude}
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
        metadata_json=metadata,
    )
    db.add(event)
    db.flush()
    db.refresh(event)
    return event


def _circle_to_geojson_polygon(
    center_lat: float,
    center_lng: float,
    radius_meters: float,
    segments: int = 48,
) -> dict[str, Any]:
    """Approximate a circle as a GeoJSON Polygon (mirrors the client's circleToGeoJsonPolygon)."""
    import math

    earth_radius = 6371000.0
    lat_rad = math.radians(center_lat)
    ring: list[list[float]] = []
    for i in range(segments):
        angle = (2 * math.pi * i) / segments
        d_lat = (radius_meters * math.cos(angle)) / earth_radius
        d_lng = (radius_meters * math.sin(angle)) / (
            earth_radius * math.cos(lat_rad)
        )
        new_lat = center_lat + math.degrees(d_lat)
        new_lng = center_lng + math.degrees(d_lng)
        ring.append([new_lng, new_lat])
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _read_circle_radius_meters(config: dict[str, Any]) -> float | None:
    """Pull a circle/proximity radius (meters) from a zone config bag."""
    for key in ("radius_meters", "resolved_radius_meters"):
        v = config.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    radii = config.get("radii_meters")
    if isinstance(radii, list) and radii:
        first = radii[0]
        if isinstance(first, (int, float)) and first > 0:
            return float(first)
    return None


def _geometry_geojson_from_parameters(
    parameters: dict[str, Any],
) -> dict[str, Any] | None:
    """Build drawable GeoJSON from a zone's stored parameters.

    Circle/proximity zones persist only ``geometry.center`` + ``config.radius_meters``
    (no ``geo_fence_polygon``), so ST_AsGeoJSON returns nothing for them. Convert the
    circle to a polygon here. Also accept a Polygon/MultiPolygon stored directly under
    ``geometry`` as a fallback for polygon zones whose PostGIS column is unset.
    """
    geometry = parameters.get("geometry") if isinstance(parameters.get("geometry"), dict) else {}
    config = parameters.get("config") if isinstance(parameters.get("config"), dict) else {}

    # Polygon/MultiPolygon stored inline.
    gtype = geometry.get("type")
    if gtype in ("Polygon", "MultiPolygon") and geometry.get("coordinates"):
        return geometry
    inner = geometry.get("geo_fence_polygon")
    if isinstance(inner, dict) and inner.get("type") in ("Polygon", "MultiPolygon"):
        return inner

    # Circle: center (lat/lng) + radius.
    center = geometry.get("center") if isinstance(geometry.get("center"), dict) else {}
    lat = center.get("latitude", center.get("lat"))
    lng = center.get("longitude", center.get("lng"))
    radius = _read_circle_radius_meters(config)
    if (
        isinstance(lat, (int, float))
        and isinstance(lng, (int, float))
        and radius is not None
    ):
        return _circle_to_geojson_polygon(float(lat), float(lng), radius)
    return None


def _geometry_center_from_parameters(
    parameters: dict[str, Any],
) -> dict[str, float] | None:
    """Read an explicit ``geometry.center`` (circle/marker zones) as {lat, lng}."""
    geometry = parameters.get("geometry") if isinstance(parameters.get("geometry"), dict) else {}
    center = geometry.get("center") if isinstance(geometry.get("center"), dict) else {}
    lat = center.get("latitude", center.get("lat"))
    lng = center.get("longitude", center.get("lng"))
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return {"lat": float(lat), "lng": float(lng)}
    return None


def _geojson_polygon_centroid(geojson: dict[str, Any]) -> dict[str, float] | None:
    """Rough centroid from the first ring of a GeoJSON Polygon/MultiPolygon."""
    try:
        gtype = geojson.get("type")
        coords = geojson.get("coordinates")
        ring: Any = None
        if gtype == "Polygon" and coords:
            ring = coords[0]
        elif gtype == "MultiPolygon" and coords:
            ring = coords[0][0]
        if not ring:
            return None
        lngs = [float(p[0]) for p in ring]
        lats = [float(p[1]) for p in ring]
        if not lats or not lngs:
            return None
        return {"lat": sum(lats) / len(lats), "lng": sum(lngs) / len(lngs)}
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _serialize_zone_like_member(
    z: Zone,
    geo_fence_geojson: dict[str, Any] | None,
) -> dict[str, Any]:
    """Serialize a zone in the SAME shape members receive from GET /zones.

    Mirrors `app.routers.zones._serialize_zone`: geometry from `parameters.geometry`
    plus the GeoJSON `geo_fence_polygon`, and `config.h3_cells` sourced from the
    canonical `zones.h3_cells` column. The guest receives a read-only copy so the
    map renders identically to the owner's, with no editing capability.
    """
    params = z.parameters if isinstance(z.parameters, dict) else {}
    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    geometry = dict(geometry)
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    config = dict(config)

    if "h3Cells" in config and "h3_cells" not in config:
        config["h3_cells"] = config["h3Cells"]
    if z.h3_cells and "h3_cells" not in config:
        config["h3_cells"] = list(z.h3_cells)

    # Prefer the serialized PostGIS polygon; fall back to one already inlined in
    # parameters.geometry (covers DBs without spatial fns).
    if geo_fence_geojson and "geo_fence_polygon" not in geometry:
        geometry["geo_fence_polygon"] = geo_fence_geojson

    return {
        "id": z.id,
        "zone_id": z.zone_id,
        "owner_id": z.owner_id,
        "name": z.name,
        "type": params.get("contractType"),
        "geometry": geometry,
        "config": config,
        "h3_cells": list(z.h3_cells) if z.h3_cells else [],
    }


def get_guest_dashboard_safe(db: Session, *, zone_id: str) -> dict[str, Any]:
    # Load the zone with its polygon serialized to GeoJSON (ST_AsGeoJSON) so a
    # geofence circle/polygon zone (which stores geometry, not h3_cells) still
    # gets a drawable map on the guest dashboard. ST_AsGeoJSON requires a spatial
    # backend (PostGIS); fall back to a plain load if it is unavailable.
    z: Zone | None = None
    geo_fence_geojson: dict[str, Any] | None = None
    zid = zone_id.strip()
    try:
        row = (
            db.query(Zone, func.ST_AsGeoJSON(Zone.geo_fence_polygon))
            .filter(Zone.zone_id == zid, Zone.active.is_(True))
            .order_by(Zone.id.asc())
            .first()
        )
        z = row[0] if row else None
        if row and row[1]:
            parsed = json.loads(row[1])
            if isinstance(parsed, dict) and parsed.get("type") in (
                "Polygon",
                "MultiPolygon",
            ):
                geo_fence_geojson = parsed
    except (TypeError, ValueError):
        geo_fence_geojson = None
    except Exception:  # pragma: no cover - spatial fn unavailable / driver error
        db.rollback()
        z = (
            db.query(Zone)
            .filter(Zone.zone_id == zid, Zone.active.is_(True))
            .order_by(Zone.id.asc())
            .first()
        )
        geo_fence_geojson = None
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

    # Circle/proximity zones store geometry as center + radius in `parameters`
    # (NOT as geo_fence_polygon), so ST_AsGeoJSON above returns nothing for them.
    # Build a drawable polygon from the stored parameters as a final fallback.
    geometry_geojson: dict[str, Any] | None = None
    geometry_center: dict[str, float] | None = None
    if z:
        zone_params = z.parameters if isinstance(z.parameters, dict) else {}
        geometry_geojson = _geometry_geojson_from_parameters(zone_params)
        geometry_center = _geometry_center_from_parameters(zone_params)

    # Read-only copy of the zone in the SAME shape members get from GET /zones,
    # so a guest can render the identical map (geometry / config.h3_cells /
    # geo_fence_polygon) without any editing capability.
    zone_serialized = _serialize_zone_like_member(z, geo_fence_geojson) if z else None

    # Prefer an explicit guest_map override; otherwise fall back to the zone's
    # actual geofence polygon (covers polygon zones), then to the geometry stored
    # in parameters (covers circle/proximity zones that have no cells/polygon).
    effective_geojson = geojson_fc or geo_fence_geojson or geometry_geojson
    if center is None and geo_fence_geojson is not None:
        center = _geojson_polygon_centroid(geo_fence_geojson)
    if center is None and geometry_center is not None:
        center = geometry_center
    if center is None and geometry_geojson is not None:
        center = _geojson_polygon_centroid(geometry_geojson)

    map_payload = {
        "center": center,
        "zoom": zoom_level,
        "cells": cells,
        **({"bounds": bounds} if bounds else {}),
        **({"geojson": effective_geojson} if effective_geojson else {}),
    }

    return {
        "zone_id": zone_id.strip(),
        "label": label,
        "welcome_text": welcome,
        "links": [],
        "cells": cells,
        "map": map_payload,
        **({"zone": zone_serialized} if zone_serialized else {}),
    }
