"""In-memory guest↔admin voice-call signaling state.

Calls are relayed over the existing realtime WebSocket. Media (WebRTC) is
negotiated client-side via CALL_SIGNAL frames; this module only tracks
ringing/connected state, ring timeouts, and access-reject escalation close.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

CallState = Literal["ringing", "connected", "ended"]

RING_TIMEOUT_SECONDS = 30


@dataclass
class GuestCallSession:
    call_id: str
    zone_id: str
    guest_id: str
    guest_name: str
    admin_owner_id: int
    caller_role: Literal["guest", "admin"]
    fullscreen: bool = False
    access_reject_escalation: bool = False
    admin_avatar_url: str | None = None
    guest_avatar_url: str | None = None
    state: CallState = "ringing"
    ring_timeout_seconds: int = RING_TIMEOUT_SECONDS
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    accepted_at: datetime | None = None
    timeout_handle: asyncio.TimerHandle | None = None
    # Guest-originated calls stay hidden from admins until the guest card is up.
    admin_invited: bool = False
    watcher_ids: list[str] = field(default_factory=list)


@dataclass
class QueuedCallRequest:
    """A call waiting for the zone line, same FIFO idea as pending guest chat."""

    queue_id: str
    zone_id: str
    guest_id: str
    guest_name: str
    admin_owner_id: int
    caller_role: Literal["guest", "admin"]
    fullscreen: bool = False
    access_reject_escalation: bool = False
    admin_avatar_url: str | None = None
    guest_avatar_url: str | None = None
    ring_timeout_seconds: int = RING_TIMEOUT_SECONDS
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_calls_by_id: dict[str, GuestCallSession] = {}
_active_by_guest: dict[str, str] = {}  # guest_id -> call_id
_queue_by_zone: dict[str, list[QueuedCallRequest]] = {}
_call_lock = asyncio.Lock()


def _utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def public_call_payload(call: GuestCallSession, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "call_id": call.call_id,
        "zone_id": call.zone_id,
        "guest_id": call.guest_id,
        "guest_name": call.guest_name,
        "admin_owner_id": call.admin_owner_id,
        "caller_role": call.caller_role,
        "fullscreen": bool(call.fullscreen),
        "access_reject_escalation": bool(call.access_reject_escalation),
        "admin_avatar_url": call.admin_avatar_url,
        "guest_avatar_url": call.guest_avatar_url,
        "state": call.state,
        "created_at": _utc_iso(call.created_at),
        "accepted_at": _utc_iso(call.accepted_at),
        "ring_timeout_seconds": int(call.ring_timeout_seconds or RING_TIMEOUT_SECONDS),
    }
    body.update(extra)
    return body


def get_call(call_id: str) -> GuestCallSession | None:
    return _calls_by_id.get((call_id or "").strip())


def active_call_for_guest(guest_id: str) -> GuestCallSession | None:
    cid = _active_by_guest.get((guest_id or "").strip())
    if not cid:
        return None
    return _calls_by_id.get(cid)


def active_call_for_zone(zone_id: str) -> GuestCallSession | None:
    zid = (zone_id or "").strip()
    if not zid:
        return None
    for call in _calls_by_id.values():
        if call.zone_id == zid and call.state in ("ringing", "connected"):
            return call
    return None


def active_call_for_admin(admin_owner_id: int) -> GuestCallSession | None:
    try:
        oid = int(admin_owner_id)
    except (TypeError, ValueError):
        return None
    for call in _calls_by_id.values():
        if call.admin_owner_id == oid and call.state in ("ringing", "connected"):
            return call
    return None


def _cancel_timeout(call: GuestCallSession) -> None:
    handle = call.timeout_handle
    call.timeout_handle = None
    if handle is not None:
        try:
            handle.cancel()
        except Exception:
            pass


def _drop_call(call: GuestCallSession) -> None:
    _cancel_timeout(call)
    _calls_by_id.pop(call.call_id, None)
    if _active_by_guest.get(call.guest_id) == call.call_id:
        _active_by_guest.pop(call.guest_id, None)


def party_user_ids(call: GuestCallSession) -> list[str]:
    ids = [str(call.admin_owner_id), f"guest:{call.guest_id}"]
    for watcher in call.watcher_ids:
        token = str(watcher).strip()
        if token and token not in ids:
            ids.append(token)
    return ids


async def create_call(
    *,
    zone_id: str,
    guest_id: str,
    guest_name: str,
    admin_owner_id: int,
    caller_role: Literal["guest", "admin"],
    fullscreen: bool = False,
    access_reject_escalation: bool = False,
    admin_avatar_url: str | None = None,
    guest_avatar_url: str | None = None,
    ring_timeout_seconds: int = RING_TIMEOUT_SECONDS,
) -> tuple[GuestCallSession | None, str | None]:
    """Create a ringing call. Returns (call, error_code)."""
    gid = (guest_id or "").strip()
    zid = (zone_id or "").strip()
    if not gid or not zid or not admin_owner_id:
        return None, "INVALID_CALL"
    existing = active_call_for_guest(gid)
    if existing and existing.state in ("ringing", "connected"):
        return None, "CALL_ALREADY_ACTIVE"

    call = GuestCallSession(
        call_id=uuid.uuid4().hex,
        zone_id=zid,
        guest_id=gid,
        guest_name=(guest_name or "").strip() or "Guest",
        admin_owner_id=int(admin_owner_id),
        caller_role=caller_role,
        fullscreen=bool(fullscreen),
        access_reject_escalation=bool(access_reject_escalation),
        admin_avatar_url=(admin_avatar_url or "").strip() or None,
        guest_avatar_url=(guest_avatar_url or "").strip() or None,
        ring_timeout_seconds=max(15, min(45, int(ring_timeout_seconds or RING_TIMEOUT_SECONDS))),
    )
    call.admin_invited = caller_role == "admin"
    _calls_by_id[call.call_id] = call
    _active_by_guest[gid] = call.call_id

    loop = asyncio.get_running_loop()
    delay = call.ring_timeout_seconds

    def _fire_timeout() -> None:
        asyncio.create_task(_timeout_call(call.call_id))

    call.timeout_handle = loop.call_later(delay, _fire_timeout)
    return call, None


async def _timeout_call(call_id: str) -> None:
    call = get_call(call_id)
    if not call or call.state != "ringing":
        return
    ended, payload = await end_call(call_id, reason="timeout", closer_role="system")
    if ended is None or payload is None:
        return
    await broadcast_call_event("CALL_TIMEOUT", ended, payload)
    if payload.get("auto_close_access"):
        await auto_close_escalated_access(zone_id=ended.zone_id, guest_id=ended.guest_id)
    started = await promote_ready_queues()
    for nxt in started:
        await deliver_live_call(nxt, from_queue=True)


async def accept_call(call_id: str, *, accepter_role: Literal["guest", "admin"]) -> tuple[GuestCallSession | None, str | None]:
    call = get_call(call_id)
    if not call:
        return None, "CALL_NOT_FOUND"
    if call.state != "ringing":
        return None, "INVALID_STATE"
    # Only the callee accepts.
    if call.caller_role == accepter_role:
        return None, "FORBIDDEN"
    _cancel_timeout(call)
    call.state = "connected"
    call.accepted_at = datetime.now(timezone.utc)
    return call, None


async def end_call(
    call_id: str,
    *,
    reason: Literal["reject", "cancel", "timeout", "hangup", "access_closed"],
    closer_role: Literal["guest", "admin", "system"],
) -> tuple[GuestCallSession | None, dict[str, Any] | None]:
    """End a call. Returns (call_snapshot_before_drop, broadcast_payload extras)."""
    call = get_call(call_id)
    if not call:
        return None, None
    if call.state == "ended":
        _drop_call(call)
        return call, None

    was_ringing = call.state == "ringing"
    call.state = "ended"
    payload = public_call_payload(
        call,
        reason=reason,
        closer_role=closer_role,
        auto_close_access=bool(
            call.access_reject_escalation
            and was_ringing
            and reason in ("reject", "timeout", "cancel")
        ),
    )
    _drop_call(call)
    return call, payload


def remember_watchers(call: GuestCallSession, user_ids: list[int | str]) -> None:
    for raw in user_ids:
        token = str(raw).strip()
        if token and token not in call.watcher_ids:
            call.watcher_ids.append(token)


async def release_admin_invite(call: GuestCallSession) -> None:
    """Show the call to admins only after the guest (sender) already has the card."""
    if call.admin_invited or call.state != "ringing" or call.caller_role != "guest":
        return
    call.admin_invited = True
    from app.websocket.manager import ws_manager

    ids = [token for token in call.watcher_ids if token and not token.startswith("guest:")]
    admin_id = str(call.admin_owner_id)
    if admin_id not in ids:
        ids.append(admin_id)
    if not ids:
        return
    await ws_manager.broadcast_to_users(ids, "CALL_INVITE", public_call_payload(call))


async def broadcast_call_event(
    event_type: str,
    call: GuestCallSession,
    payload: dict[str, Any],
    extra_user_ids: list[int | str] | None = None,
) -> None:
    from app.websocket.manager import ws_manager

    ids = party_user_ids(call)
    for extra in extra_user_ids or []:
        token = str(extra).strip()
        if token and token not in ids:
            ids.append(token)
    await ws_manager.broadcast_to_users(ids, event_type, payload)


def _queue_for(zone_id: str) -> list[QueuedCallRequest]:
    zid = (zone_id or "").strip()
    bucket = _queue_by_zone.get(zid)
    if bucket is None:
        bucket = []
        _queue_by_zone[zid] = bucket
    return bucket


def queue_position(item: QueuedCallRequest) -> int:
    bucket = _queue_by_zone.get(item.zone_id) or []
    for index, queued in enumerate(bucket, start=1):
        if queued.queue_id == item.queue_id:
            return index
    return 1


def queued_call_for_guest(guest_id: str) -> QueuedCallRequest | None:
    gid = (guest_id or "").strip()
    if not gid:
        return None
    for bucket in _queue_by_zone.values():
        for item in bucket:
            if item.guest_id == gid:
                return item
    return None


def queued_call_for_admin(admin_owner_id: int) -> QueuedCallRequest | None:
    try:
        oid = int(admin_owner_id)
    except (TypeError, ValueError):
        return None
    for bucket in _queue_by_zone.values():
        for item in bucket:
            if item.admin_owner_id == oid and item.caller_role == "admin":
                return item
    return None


def public_queue_payload(item: QueuedCallRequest, *, position: int | None = None) -> dict[str, Any]:
    pos = queue_position(item) if position is None else int(position)
    return {
        "call_id": item.queue_id,
        "queue_id": item.queue_id,
        "queue_position": pos,
        "zone_id": item.zone_id,
        "guest_id": item.guest_id,
        "guest_name": item.guest_name,
        "admin_owner_id": item.admin_owner_id,
        "caller_role": item.caller_role,
        "fullscreen": bool(item.fullscreen),
        "access_reject_escalation": bool(item.access_reject_escalation),
        "admin_avatar_url": item.admin_avatar_url,
        "guest_avatar_url": item.guest_avatar_url,
        "state": "queued",
        "created_at": _utc_iso(item.enqueued_at),
        "accepted_at": None,
    }


def _find_queued(guest_id: str, zone_id: str) -> QueuedCallRequest | None:
    gid = (guest_id or "").strip()
    for item in _queue_for(zone_id):
        if item.guest_id == gid:
            return item
    return None


def _line_busy(zone_id: str, admin_owner_id: int) -> bool:
    if active_call_for_zone(zone_id):
        return True
    if active_call_for_admin(admin_owner_id):
        return True
    return False


async def enqueue_or_start(
    *,
    zone_id: str,
    guest_id: str,
    guest_name: str,
    admin_owner_id: int,
    caller_role: Literal["guest", "admin"],
    fullscreen: bool = False,
    access_reject_escalation: bool = False,
    admin_avatar_url: str | None = None,
    guest_avatar_url: str | None = None,
    ring_timeout_seconds: int = RING_TIMEOUT_SECONDS,
) -> tuple[GuestCallSession | None, QueuedCallRequest | None, str | None, list[GuestCallSession]]:
    """Start a call, or park it until the current zone call ends.

    Returns ``(active_call, queued_request, error, promoted)``. Exactly one of
    the first two is set when ``error`` is ``None``. ``promoted`` holds calls
    that were waiting and started because the line was free.
    """
    async with _call_lock:
        gid = (guest_id or "").strip()
        zid = (zone_id or "").strip()
        promoted: list[GuestCallSession] = []
        if not gid or not zid or not admin_owner_id:
            return None, None, "INVALID_CALL", promoted
        existing = active_call_for_guest(gid)
        if existing and existing.state in ("ringing", "connected"):
            return None, None, "CALL_ALREADY_ACTIVE", promoted
        already = _find_queued(gid, zid)
        if already is not None:
            return None, already, None, promoted
        if not _line_busy(zid, int(admin_owner_id)):
            promoted = await _promote_holding_lock()
        if _find_queued(gid, zid) is not None:
            return None, _find_queued(gid, zid), None, promoted
        if _line_busy(zid, int(admin_owner_id)) or _queue_for(zid):
            item = QueuedCallRequest(
                queue_id=f"queue-{uuid.uuid4().hex}",
                zone_id=zid,
                guest_id=gid,
                guest_name=(guest_name or "").strip() or "Guest",
                admin_owner_id=int(admin_owner_id),
                caller_role=caller_role,
                fullscreen=bool(fullscreen),
                access_reject_escalation=bool(access_reject_escalation),
                admin_avatar_url=(admin_avatar_url or "").strip() or None,
                guest_avatar_url=(guest_avatar_url or "").strip() or None,
                ring_timeout_seconds=max(5, int(ring_timeout_seconds or RING_TIMEOUT_SECONDS)),
            )
            _queue_for(zid).append(item)
            return None, item, None, promoted
        call, err = await create_call(
            zone_id=zid,
            guest_id=gid,
            guest_name=guest_name,
            admin_owner_id=int(admin_owner_id),
            caller_role=caller_role,
            fullscreen=fullscreen,
            access_reject_escalation=access_reject_escalation,
            admin_avatar_url=admin_avatar_url,
            guest_avatar_url=guest_avatar_url,
            ring_timeout_seconds=ring_timeout_seconds,
        )
        return call, None, err, promoted


async def deliver_live_call(call: GuestCallSession, *, from_queue: bool = False) -> None:
    """One event for the caller, one invite for the receiver.

    The caller already has the card open. ``CALL_STARTED`` only swaps in the
    server id. ``CALL_INVITE`` is what shows the card on the other side.
    """
    from app.database import session_maker
    from app.services import guest_access_service as gas
    from app.websocket.manager import ws_manager

    if call.caller_role == "guest":
        has_admin = any(not str(item).startswith("guest:") for item in call.watcher_ids)
        if not has_admin:
            db = session_maker()
            try:
                remember_watchers(call, gas.zone_member_owner_ids(db, call.zone_id))
            finally:
                db.close()
    call.admin_invited = True
    payload = public_call_payload(call, **({"from_queue": True} if from_queue else {}))
    if call.caller_role == "guest":
        receiver_ids = [token for token in call.watcher_ids if token and not token.startswith("guest:")]
        admin_id = str(call.admin_owner_id)
        if admin_id not in receiver_ids:
            receiver_ids.append(admin_id)
        await ws_manager.broadcast_to_users(receiver_ids, "CALL_INVITE", payload)
        await ws_manager.broadcast_to_users([f"guest:{call.guest_id}"], "CALL_STARTED", payload)
        return
    await ws_manager.broadcast_to_users([f"guest:{call.guest_id}"], "CALL_INVITE", payload)
    await ws_manager.broadcast_to_users([str(call.admin_owner_id)], "CALL_STARTED", payload)


async def _promote_holding_lock() -> list[GuestCallSession]:
    """Start the next waiting call on each free zone line. Caller holds ``_call_lock``."""
    started: list[GuestCallSession] = []
    for zone_id in list(_queue_by_zone.keys()):
        bucket = _queue_by_zone.get(zone_id) or []
        while bucket:
            if active_call_for_zone(zone_id):
                break
            item = bucket[0]
            if active_call_for_admin(item.admin_owner_id):
                break
            bucket.pop(0)
            call, err = await create_call(
                zone_id=item.zone_id,
                guest_id=item.guest_id,
                guest_name=item.guest_name,
                admin_owner_id=item.admin_owner_id,
                caller_role=item.caller_role,
                fullscreen=item.fullscreen,
                access_reject_escalation=item.access_reject_escalation,
                admin_avatar_url=item.admin_avatar_url,
                guest_avatar_url=item.guest_avatar_url,
                ring_timeout_seconds=item.ring_timeout_seconds,
            )
            if call and not err:
                started.append(call)
                break
            logger.info(
                "skipped queued call guest_id=%s err=%s",
                item.guest_id,
                err,
            )
        if not bucket:
            _queue_by_zone.pop(zone_id, None)
    return started


async def promote_ready_queues() -> list[GuestCallSession]:
    """Pop one waiting call per free zone line and start it."""
    async with _call_lock:
        return await _promote_holding_lock()


async def cancel_queued_call(
    queue_id: str,
    *,
    guest_id: str | None = None,
    admin_owner_id: int | None = None,
) -> QueuedCallRequest | None:
    """Remove a waiting call. Only the party who requested it may cancel."""
    qid = (queue_id or "").strip()
    if not qid:
        return None
    async with _call_lock:
        for zone_id, bucket in list(_queue_by_zone.items()):
            for index, item in enumerate(bucket):
                if item.queue_id != qid:
                    continue
                if item.caller_role == "guest":
                    if (guest_id or "").strip() != item.guest_id:
                        return None
                elif admin_owner_id is None or int(admin_owner_id) != item.admin_owner_id:
                    return None
                bucket.pop(index)
                if not bucket:
                    _queue_by_zone.pop(zone_id, None)
                return item
    return None


async def notify_removed_queue(removed: list[QueuedCallRequest]) -> None:
    if not removed:
        return
    from app.websocket.manager import ws_manager

    for item in removed:
        payload = public_queue_payload(item, position=0)
        payload["reason"] = "cancel"
        await ws_manager.broadcast_to_users(
            [f"guest:{item.guest_id}", str(item.admin_owner_id)],
            "CALL_CANCEL",
            payload,
        )


def remove_queued_for_guest(guest_id: str) -> list[QueuedCallRequest]:
    """Drop waiting calls for a guest whose access was approved or rejected."""
    gid = (guest_id or "").strip()
    removed: list[QueuedCallRequest] = []
    if not gid:
        return removed
    for zone_id, bucket in list(_queue_by_zone.items()):
        keep: list[QueuedCallRequest] = []
        for item in bucket:
            if item.guest_id == gid:
                removed.append(item)
            else:
                keep.append(item)
        if keep:
            _queue_by_zone[zone_id] = keep
        else:
            _queue_by_zone.pop(zone_id, None)
    return removed


async def auto_close_escalated_access(*, zone_id: str, guest_id: str) -> bool:
    """Reject the pending access request when an escalated 4th-request call fails."""
    from app.database import session_maker
    from app.models.owner import Owner, OwnerRole
    from app.services import guest_access_service as gas
    from app.websocket.manager import ws_manager

    db = session_maker()
    try:
        row = gas.get_guest_access_session_by_guest_id(db, guest_id)
        if not row or row.zone_id != zone_id.strip():
            return False
        if row.resolution != "pending":
            return False
        admin = None
        if row.admin_owner_id:
            admin = db.query(Owner).filter(Owner.id == row.admin_owner_id).first()
        if admin is None:
            admin = gas.resolve_primary_zone_admin_owner(db, zone_id)
        if admin is None:
            admin = (
                db.query(Owner)
                .filter(
                    Owner.zone_id == zone_id.strip(),
                    Owner.role == OwnerRole.ADMINISTRATOR,
                    Owner.active.is_(True),
                )
                .first()
            )
        if admin is None:
            return False
        result = gas.reject_guest(
            db,
            acting_owner=admin,
            zone_id=zone_id.strip(),
            guest_id=guest_id.strip(),
        )
        if result.get("error"):
            logger.info(
                "auto_close_escalated_access failed guest_id=%s err=%s",
                guest_id,
                result.get("error"),
            )
            return False
        db.commit()
        notify_ids = gas.zone_member_owner_ids(db, zone_id.strip())
        if notify_ids:
            await ws_manager.broadcast_to_users(
                notify_ids,
                "GUEST_REQUEST_CHANGED",
                {
                    "guest_id": guest_id.strip(),
                    "zone_id": zone_id.strip(),
                    "status": "REJECTED",
                    "auto_closed": True,
                    "reason": "escalated_call_failed",
                },
            )
        await ws_manager.broadcast_to_users(
            [f"guest:{guest_id.strip()}"],
            "GUEST_REQUEST_CHANGED",
            {
                "guest_id": guest_id.strip(),
                "zone_id": zone_id.strip(),
                "status": "REJECTED",
                "auto_closed": True,
                "reason": "escalated_call_failed",
            },
        )
        return True
    except Exception:
        logger.exception("auto_close_escalated_access crashed guest_id=%s", guest_id)
        db.rollback()
        return False
    finally:
        db.close()
