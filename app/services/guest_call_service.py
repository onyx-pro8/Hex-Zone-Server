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
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    accepted_at: datetime | None = None
    timeout_handle: asyncio.TimerHandle | None = None


_calls_by_id: dict[str, GuestCallSession] = {}
_active_by_guest: dict[str, str] = {}  # guest_id -> call_id


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
        "ring_timeout_seconds": RING_TIMEOUT_SECONDS,
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
    return [str(call.admin_owner_id), f"guest:{call.guest_id}"]


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
    )
    _calls_by_id[call.call_id] = call
    _active_by_guest[gid] = call.call_id

    loop = asyncio.get_running_loop()
    delay = max(5, int(ring_timeout_seconds or RING_TIMEOUT_SECONDS))

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


async def broadcast_call_event(event_type: str, call: GuestCallSession, payload: dict[str, Any]) -> None:
    from app.websocket.manager import ws_manager

    await ws_manager.broadcast_to_users(party_user_ids(call), event_type, payload)


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
