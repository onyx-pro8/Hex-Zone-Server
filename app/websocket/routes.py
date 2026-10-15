"""Websocket endpoint for realtime zone subscriptions.

After **`?token=`** JWT auth, clients send JSON text frames:

- **`type=SUBSCRIBE`** with **`zoneIds`** array — zone fan-out for messages.
- **`type=LOCATION_UPDATE`** with **`latitude`/`longitude`** (top-level or under **`data`**) —
  upserts live GPS via the same path as **`POST /message-feature/members/location`**.
  Guest tokens cannot send location updates.

Server sends **`type`** + **`data`** envelopes. Common **`type`** values:

- **`NEW_MESSAGE`** — **`data`** matches **`ZoneMessageResponse`** JSON.
- **`NEW_GEO_MESSAGE`**, **`PERMISSION_MESSAGE`**, **`WELLNESS_ACK`**
- **`guest_is_here`**, **`unexpected_guest`**, **`GUEST_REQUEST_CHANGED`**
- **`BLOCKS_CHANGED`** — block-rule create/delete for the owning user
- **`SESSION_REVOKED`** — another device claimed the account session
- **`MEMBER_PRESENCE`** — `{ owner_id, online }` when a member connects/disconnects
- **`LOCATION_UPDATE_ACK`** — ack after a successful **`LOCATION_UPDATE`**
- **`guest_zone_message`** — guest-thread CHAT push (member or guest clients)
- **`GUEST_PRESENCE`** — `{ guest_id, online }` when a guest connects/disconnects WS or hits `/api/guest/*`
- **`CALL_INVITE`**, **`CALL_ACCEPT`**, **`CALL_REJECT`**, **`CALL_CANCEL`**,
  **`CALL_TIMEOUT`**, **`CALL_END`**, **`CALL_SIGNAL`** — guest↔admin voice call signaling

Clients may also **send** call frames (same type names) after auth; the server relays
them to the other party. Auth: member JWT (**`sub`** = owner id) or guest JWT
(**`token_use=guest_access`**, **`sub=guest:{guest_id}`**).
"""
import json
import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi import HTTPException

from app.core.security import verify_token
from app.crud import owner as owner_crud
from app.database import session_maker
from app.services import guest_call_service
from app.services.member_service import upsert_member_location
from app.websocket.manager import ws_manager

router = APIRouter()
logger = logging.getLogger(__name__)


def _coords_from_frame(data: dict) -> tuple[float, float] | None:
    """Accept lat/lng at top level or nested under ``data``."""
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    latitude = data.get("latitude", nested.get("latitude") if nested else None)
    longitude = data.get("longitude", nested.get("longitude") if nested else None)
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    return float(latitude), float(longitude)


async def _publish_guest_ws_presence(guest_id: str, *, online: bool) -> None:
    """Fan out **`GUEST_PRESENCE`** to zone staff and refresh **`last_seen_at`** when online."""
    gid = (guest_id or "").strip()
    if not gid:
        return
    from app.services import guest_access_service as gas

    zone_id: str | None = None
    if online:
        zone_id, _ = gas.touch_guest_last_seen(gid)
    else:
        db = session_maker()
        try:
            row = gas.get_guest_access_session_by_guest_id(db, gid)
            zone_id = row.zone_id if row else None
        finally:
            db.close()
    if not zone_id:
        return
    db = session_maker()
    try:
        staff = gas.zone_staff_owner_ids(db, zone_id)
    finally:
        db.close()
    if not staff:
        return
    await ws_manager.broadcast_to_users(
        sorted(staff),
        "GUEST_PRESENCE",
        {"guest_id": gid, "online": bool(online)},
    )


def _owner_avatar_url(db, owner_id: int | None) -> str | None:
    if not owner_id:
        return None
    from app.models.owner import Owner

    owner = db.get(Owner, int(owner_id))
    if owner is None:
        return None
    url = str(getattr(owner, "avatar_url", "") or "").strip()
    return url or None


async def _resolve_call_admin_for_guest(guest_id: str) -> tuple[str | None, str | None, int | None, str | None]:
    """Return (zone_id, guest_name, admin_owner_id, admin_avatar_url)."""
    from app.services import guest_access_service as gas

    db = session_maker()
    try:
        row = gas.get_guest_access_session_by_guest_id(db, guest_id)
        if not row:
            return None, None, None, None
        admin_id = row.admin_owner_id
        if admin_id is None:
            admin = gas.resolve_primary_zone_admin_owner(db, row.zone_id)
            admin_id = admin.id if admin else None
        avatar = _owner_avatar_url(db, int(admin_id) if admin_id else None)
        return (
            row.zone_id,
            (row.guest_name or "").strip() or "Guest",
            int(admin_id) if admin_id else None,
            avatar,
        )
    finally:
        db.close()


async def _announce_queued(item: guest_call_service.QueuedCallRequest) -> None:
    """Tell only the waiting caller. The other party is invited when the line frees."""
    payload = guest_call_service.public_queue_payload(item)
    target = (
        [f"guest:{item.guest_id}"]
        if item.caller_role == "guest"
        else [str(item.admin_owner_id)]
    )
    await ws_manager.broadcast_to_users(target, "CALL_QUEUED", payload)


async def _broadcast_promoted(started: list[guest_call_service.GuestCallSession]) -> None:
    for call in started:
        await guest_call_service.deliver_live_call(call, from_queue=True)


async def _finish_call_end(
    call: guest_call_service.GuestCallSession,
    payload: dict[str, Any],
    *,
    event_type: str,
) -> None:
    await guest_call_service.broadcast_call_event(event_type, call, payload)
    if payload.get("auto_close_access"):
        await guest_call_service.auto_close_escalated_access(
            zone_id=call.zone_id,
            guest_id=call.guest_id,
        )
    started = await guest_call_service.promote_ready_queues()
    await _broadcast_promoted(started)


async def _handle_call_frame(
    websocket: WebSocket,
    *,
    is_guest: bool,
    guest_id: str | None,
    user_id: str,
    data: dict,
) -> None:
    message_type = str(data.get("type") or "").strip().upper()
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    role: Literal["guest", "admin"] = "guest" if is_guest else "admin"

    if message_type == "CALL_INVITE":
        if is_guest:
            if not guest_id:
                await websocket.send_json(
                    {"type": "ERROR", "error": {"message": "Guest identity required"}}
                )
                return
            zone_id, guest_name, admin_oid, admin_avatar = await _resolve_call_admin_for_guest(guest_id)
            client_call_id = str(body.get("call_id") or "").strip()
            if guest_call_service.consume_cancelled_client_call(client_call_id):
                if bool(body.get("access_reject_escalation")) and zone_id:
                    await guest_call_service.auto_close_escalated_access(
                        zone_id=zone_id,
                        guest_id=guest_id,
                    )
                return
            if not zone_id or not admin_oid:
                await websocket.send_json(
                    {
                        "type": "ERROR",
                        "error": {"message": "No administrator available for call"},
                    }
                )
                return
            from app.services import guest_access_service as gas

            db = session_maker()
            try:
                member_ids = [str(item) for item in gas.zone_member_owner_ids(db, zone_id)]
            finally:
                db.close()
            if str(admin_oid) not in member_ids:
                member_ids.append(str(admin_oid))
            if not any(ws_manager.is_user_connected(item) for item in member_ids):
                await websocket.send_json(
                    {
                        "type": "ERROR",
                        "error": {
                            "message": "The administrator is offline.",
                            "error_code": "RECEIVER_OFFLINE",
                        },
                    }
                )
                return
            call, queued, err, promoted = await guest_call_service.enqueue_or_start(
                zone_id=zone_id,
                guest_id=guest_id,
                guest_name=guest_name or "Guest",
                admin_owner_id=admin_oid,
                caller_role="guest",
                fullscreen=bool(body.get("fullscreen")),
                access_reject_escalation=bool(body.get("access_reject_escalation")),
                admin_avatar_url=admin_avatar,
                guest_avatar_url=str(body.get("guest_avatar_url") or "").strip() or None,
            )
            if promoted:
                await _broadcast_promoted(promoted)
            if queued is not None and err is None:
                await _announce_queued(queued)
                return
            if err == "CALL_ALREADY_ACTIVE":
                existing = guest_call_service.active_call_for_guest(guest_id)
                if existing is not None and existing.state in ("ringing", "connected"):
                    replay = "CALL_ACCEPT" if existing.state == "connected" else "CALL_STARTED"
                    await websocket.send_json(
                        {
                            "type": replay,
                            "data": guest_call_service.public_call_payload(existing),
                        }
                    )
                    return
            if call is not None and err is None:
                guest_call_service.remember_watchers(call, member_ids)
                await guest_call_service.deliver_live_call(call)
                await websocket.send_json(
                    {
                        "type": "CALL_STARTED",
                        "data": guest_call_service.public_call_payload(call),
                    }
                )
                return
        else:
            try:
                admin_oid = int(user_id)
            except (TypeError, ValueError):
                await websocket.send_json(
                    {"type": "ERROR", "error": {"message": "Invalid admin identity"}}
                )
                return
            target_guest = str(body.get("guest_id") or "").strip()
            zone_id = str(body.get("zone_id") or "").strip()
            guest_name = str(body.get("guest_name") or "").strip() or "Guest"
            client_call_id = str(body.get("call_id") or "").strip()
            if guest_call_service.consume_cancelled_client_call(client_call_id):
                return
            if not target_guest or not zone_id:
                await websocket.send_json(
                    {
                        "type": "ERROR",
                        "error": {"message": "guest_id and zone_id are required"},
                    }
                )
                return
            if not ws_manager.is_user_connected(f"guest:{target_guest}"):
                await websocket.send_json(
                    {
                        "type": "ERROR",
                        "error": {
                            "message": "The guest is offline.",
                            "error_code": "RECEIVER_OFFLINE",
                        },
                    }
                )
                return
            db = session_maker()
            try:
                admin_avatar = _owner_avatar_url(db, admin_oid)
            finally:
                db.close()
            call, queued, err, promoted = await guest_call_service.enqueue_or_start(
                zone_id=zone_id,
                guest_id=target_guest,
                guest_name=guest_name,
                admin_owner_id=admin_oid,
                caller_role="admin",
                fullscreen=bool(body.get("fullscreen")),
                access_reject_escalation=bool(body.get("access_reject_escalation")),
                admin_avatar_url=admin_avatar,
                guest_avatar_url=str(body.get("guest_avatar_url") or "").strip() or None,
            )
            if promoted:
                await _broadcast_promoted(promoted)
            if queued is not None and err is None:
                await _announce_queued(queued)
                return
        if err or call is None:
            await websocket.send_json(
                {
                    "type": "ERROR",
                    "error": {
                        "message": "Call could not be started",
                        "error_code": err or "CALL_FAILED",
                    },
                }
            )
            return
        await guest_call_service.deliver_live_call(call)
        await websocket.send_json(
            {
                "type": "CALL_STARTED",
                "data": guest_call_service.public_call_payload(call),
            }
        )
        return

    call_id = str(body.get("call_id") or "").strip()
    if not call_id:
        await websocket.send_json(
            {"type": "ERROR", "error": {"message": "call_id is required"}}
        )
        return
    call = guest_call_service.get_call(call_id)
    if not call and message_type in ("CALL_CANCEL", "CALL_REJECT", "CALL_END"):
        # Optimistic cards use a local pending id. End the real call for that guest.
        lookup_guest = guest_id if is_guest else str(body.get("guest_id") or "").strip()
        if lookup_guest:
            call = guest_call_service.active_call_for_guest(lookup_guest)
            if call is not None:
                call_id = call.call_id
    if message_type == "CALL_ARM":
        target = call or (
            guest_call_service.active_call_for_guest(guest_id)
            if is_guest and guest_id
            else None
        )
        if target is not None and is_guest and guest_id == target.guest_id:
            await guest_call_service.release_admin_invite(target)
        return
    if not call and message_type in ("CALL_CANCEL", "CALL_REJECT", "CALL_END"):
        guest_call_service.remember_cancelled_client_call(call_id)
        lookup_guest = guest_id if is_guest else str(body.get("guest_id") or "").strip()
        removed = await guest_call_service.cancel_queued_call(
            call_id,
            guest_id=guest_id if is_guest else None,
            admin_owner_id=int(user_id) if not is_guest and str(user_id).isdigit() else None,
        )
        if removed is None and lookup_guest and message_type != "CALL_END":
            try:
                admin_lookup = int(user_id) if not is_guest else None
            except (TypeError, ValueError):
                admin_lookup = None
            removed = await guest_call_service.cancel_queued_for_guest(
                lookup_guest,
                admin_owner_id=admin_lookup,
                as_guest=is_guest,
            )
        if removed is not None:
            payload = guest_call_service.public_queue_payload(removed, position=0)
            payload["reason"] = "cancel"
            if removed.access_reject_escalation or bool(body.get("access_reject_escalation")):
                payload["auto_close_access"] = True
            await ws_manager.broadcast_to_users(
                [f"guest:{removed.guest_id}", str(removed.admin_owner_id)],
                "CALL_CANCEL",
                payload,
            )
            if payload.get("auto_close_access"):
                await guest_call_service.auto_close_escalated_access(
                    zone_id=removed.zone_id,
                    guest_id=removed.guest_id,
                )
            return
        if (
            message_type in ("CALL_CANCEL", "CALL_REJECT")
            and lookup_guest
            and bool(body.get("access_reject_escalation"))
        ):
            zone_for_close = str(body.get("zone_id") or "").strip()
            if zone_for_close:
                await guest_call_service.auto_close_escalated_access(
                    zone_id=zone_for_close,
                    guest_id=lookup_guest,
                )
            return
    if not call and message_type == "CALL_SYNC":
        if is_guest and guest_id:
            call = guest_call_service.active_call_for_guest(guest_id)
        elif not is_guest and str(user_id).isdigit():
            call = guest_call_service.active_call_for_admin(int(user_id))
        if call is None:
            return
    if not call:
        await websocket.send_json(
            {
                "type": "ERROR",
                "error": {"message": "Call not found", "error_code": "CALL_NOT_FOUND"},
            }
        )
        return

    # Authorization: only the two parties may act on the call.
    if is_guest:
        if not guest_id or guest_id != call.guest_id:
            await websocket.send_json(
                {"type": "ERROR", "error": {"message": "Forbidden", "error_code": "FORBIDDEN"}}
            )
            return
    else:
        try:
            oid = int(user_id)
        except (TypeError, ValueError):
            await websocket.send_json(
                {"type": "ERROR", "error": {"message": "Forbidden", "error_code": "FORBIDDEN"}}
            )
            return
        # Zone colleagues also receive the invite. The one who answers owns the call.
        if oid != call.admin_owner_id:
            call.admin_owner_id = oid

    if message_type == "CALL_SYNC":
        synced = call or (
            guest_call_service.active_call_for_guest(guest_id)
            if is_guest and guest_id
            else None
        )
        if synced is None and not is_guest:
            try:
                synced = guest_call_service.active_call_for_admin(int(user_id))
            except (TypeError, ValueError):
                synced = None
        if synced is None or synced.state not in ("ringing", "connected"):
            return
        if synced.state == "connected":
            sync_type = "CALL_ACCEPT"
        elif (is_guest and synced.caller_role == "guest") or (
            not is_guest and synced.caller_role == "admin"
        ):
            sync_type = "CALL_STARTED"
        else:
            sync_type = "CALL_INVITE"
        await websocket.send_json(
            {
                "type": sync_type,
                "data": guest_call_service.public_call_payload(synced),
            }
        )
        return

    if message_type == "CALL_ACCEPT":
        accepted, err = await guest_call_service.accept_call(call_id, accepter_role=role)
        if err or accepted is None:
            await websocket.send_json(
                {
                    "type": "ERROR",
                    "error": {"message": "Call accept failed", "error_code": err or "CALL_FAILED"},
                }
            )
            return
        payload = guest_call_service.public_call_payload(accepted)
        await guest_call_service.broadcast_call_event("CALL_ACCEPT", accepted, payload)
        return

    if message_type == "CALL_SIGNAL":
        signal = body.get("signal")
        if not isinstance(signal, dict):
            await websocket.send_json(
                {"type": "ERROR", "error": {"message": "signal object is required"}}
            )
            return
        payload = guest_call_service.public_call_payload(
            call,
            signal=signal,
            from_role=role,
        )
        # Relay only to the other party.
        other = (
            [str(call.admin_owner_id)]
            if is_guest
            else [f"guest:{call.guest_id}"]
        )
        await ws_manager.broadcast_to_users(other, "CALL_SIGNAL", payload)
        return

    if message_type in ("CALL_REJECT", "CALL_CANCEL", "CALL_END"):
        reason_map = {
            "CALL_REJECT": "reject",
            "CALL_CANCEL": "cancel",
            "CALL_END": "hangup",
        }
        reason = reason_map[message_type]
        # Reject is for the callee while ringing; cancel is for the caller.
        if message_type == "CALL_REJECT" and call.caller_role == role:
            reason = "cancel"
            message_type = "CALL_CANCEL"
        if message_type == "CALL_CANCEL" and call.caller_role != role:
            reason = "reject"
            message_type = "CALL_REJECT"
        ended, payload = await guest_call_service.end_call(
            call_id, reason=reason, closer_role=role  # type: ignore[arg-type]
        )
        if ended is None or payload is None:
            return
        event = (
            "CALL_TIMEOUT"
            if reason == "timeout"
            else message_type
            if message_type in ("CALL_REJECT", "CALL_CANCEL", "CALL_END")
            else "CALL_END"
        )
        await _finish_call_end(ended, payload, event_type=event)
        return

    await websocket.send_json(
        {"type": "ERROR", "error": {"message": f"Unsupported call type {message_type}"}}
    )


async def _handle_location_update(websocket: WebSocket, user_id: str, data: dict) -> None:
    coords = _coords_from_frame(data)
    if coords is None:
        await websocket.send_json(
            {
                "type": "ERROR",
                "error": {"message": "latitude/longitude are required numbers"},
            }
        )
        return

    latitude, longitude = coords
    try:
        owner_pk = int(user_id)
    except (TypeError, ValueError):
        await websocket.send_json(
            {"type": "ERROR", "error": {"message": "Invalid user identity"}}
        )
        return

    db = session_maker()
    try:
        owner = owner_crud.get_owner(db, owner_pk)
        if not owner:
            await websocket.send_json(
                {"type": "ERROR", "error": {"message": "Owner not found"}}
            )
            return
        matched = upsert_member_location(db, owner.id, latitude, longitude)
        db.commit()
        await websocket.send_json(
            {
                "type": "LOCATION_UPDATE_ACK",
                "data": {"zone_ids": matched.get("zones") or []},
            }
        )
    except Exception:
        db.rollback()
        logger.exception("WebSocket LOCATION_UPDATE failed: user_id=%s", user_id)
        await websocket.send_json(
            {"type": "ERROR", "error": {"message": "Failed to update location"}}
        )
    finally:
        db.close()


async def _zone_websocket_session(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    if not token:
        logger.warning("WebSocket auth failed: missing token")
        await websocket.close(code=1008, reason="Missing token")
        return
    try:
        payload = verify_token(token)
    except HTTPException:
        logger.warning("WebSocket auth failed: invalid token")
        await websocket.close(code=1008, reason="Invalid token")
        return
    user_id = str(payload.get("sub") or "")
    if not user_id or user_id == "None":
        logger.warning("WebSocket auth failed: invalid subject in token")
        await websocket.close(code=1008, reason="Invalid token")
        return

    is_guest = payload.get("token_use") == "guest_access" or user_id.startswith("guest:")
    guest_id: str | None = None
    guest_allowed_zones: set[str] = set()
    if is_guest:
        if payload.get("token_use") != "guest_access" or not user_id.startswith("guest:"):
            logger.warning("WebSocket auth failed: invalid guest token shape")
            await websocket.close(code=1008, reason="Invalid token")
            return
        guest_id = user_id[len("guest:") :].strip()
        if not guest_id:
            await websocket.close(code=1008, reason="Invalid token")
            return
        raw_zones = payload.get("zone_ids") or []
        if isinstance(raw_zones, list):
            guest_allowed_zones = {
                str(z).strip() for z in raw_zones if str(z).strip()
            }
        db = session_maker()
        try:
            from app.services import guest_access_service as gas

            gas.require_guest_bearer_session_active(db, guest_id=guest_id, for_call=True)
        except HTTPException:
            await websocket.close(code=1008, reason="Guest access invalidated")
            return
        except Exception:
            logger.exception("WebSocket guest session check failed: guest_id=%s", guest_id)
            await websocket.close(code=1011, reason="Internal error")
            return
        finally:
            db.close()
        user_id = f"guest:{guest_id}"
    else:
        try:
            int(user_id)
        except (TypeError, ValueError):
            logger.warning("WebSocket auth failed: non-member subject=%s", user_id)
            await websocket.close(code=1008, reason="Invalid token")
            return

    logger.info(
        "WebSocket auth succeeded: user_id=%s is_guest=%s",
        user_id,
        is_guest,
    )
    connection_id = await ws_manager.connect(user_id, websocket)
    if ws_manager.count_connections_for_user(user_id) == 1:
        try:
            if is_guest and guest_id:
                await _publish_guest_ws_presence(guest_id, online=True)
            else:
                from app.services.member_presence_service import publish_member_presence

                await publish_member_presence(int(user_id), True)
        except Exception:
            logger.exception("Failed to publish online presence: user_id=%s", user_id)
    # Replay an in-flight call so late-joining guests/admins still see the card.
    try:
        if is_guest and guest_id:
            active = guest_call_service.active_call_for_guest(guest_id)
            if active and active.state in ("ringing", "connected"):
                # The caller already opened the card. Restore with CALL_STARTED
                # so a reconnect is not a second invite.
                if active.state == "connected":
                    replay_type = "CALL_ACCEPT"
                elif active.caller_role == "guest":
                    replay_type = "CALL_STARTED"
                else:
                    replay_type = "CALL_INVITE"
                await websocket.send_json(
                    {
                        "type": replay_type,
                        "data": guest_call_service.public_call_payload(active),
                    }
                )
            else:
                waiting = guest_call_service.queued_call_for_guest(guest_id)
                if waiting is not None:
                    await websocket.send_json(
                        {
                            "type": "CALL_QUEUED",
                            "data": guest_call_service.public_queue_payload(waiting),
                        }
                    )
        elif not is_guest:
            try:
                oid = int(user_id)
            except (TypeError, ValueError):
                oid = None
            if oid is not None:
                active_admin = guest_call_service.active_call_for_admin(oid)
                if (
                    active_admin
                    and active_admin.state in ("ringing", "connected")
                    and (active_admin.caller_role == "admin" or active_admin.admin_invited)
                ):
                    if active_admin.state == "connected":
                        admin_replay = "CALL_ACCEPT"
                    elif active_admin.caller_role == "admin":
                        admin_replay = "CALL_STARTED"
                    else:
                        admin_replay = "CALL_INVITE"
                    await websocket.send_json(
                        {
                            "type": admin_replay,
                            "data": guest_call_service.public_call_payload(active_admin),
                        }
                    )
                else:
                    waiting_admin = guest_call_service.queued_call_for_admin(oid)
                    if waiting_admin is not None:
                        await websocket.send_json(
                            {
                                "type": "CALL_QUEUED",
                                "data": guest_call_service.public_queue_payload(waiting_admin),
                            }
                        )
    except Exception:
        logger.exception("Failed to replay active call on connect: user_id=%s", user_id)
    try:
        while True:
            raw_message = await websocket.receive_text()
            try:
                data = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.warning("WebSocket invalid JSON: connection_id=%s", connection_id)
                await websocket.send_json(
                    {"type": "ERROR", "error": {"message": "Invalid JSON payload"}}
                )
                continue

            if not isinstance(data, dict):
                logger.warning("WebSocket invalid message type: connection_id=%s", connection_id)
                await websocket.send_json(
                    {"type": "ERROR", "error": {"message": "Payload must be a JSON object"}}
                )
                continue

            message_type = data.get("type")
            if message_type == "LOCATION_UPDATE":
                if is_guest:
                    await websocket.send_json(
                        {
                            "type": "ERROR",
                            "error": {
                                "message": "Guests cannot update location over WebSocket"
                            },
                        }
                    )
                    continue
                await _handle_location_update(websocket, user_id, data)
                continue

            if isinstance(message_type, str) and message_type.upper().startswith("CALL_"):
                await _handle_call_frame(
                    websocket,
                    is_guest=is_guest,
                    guest_id=guest_id,
                    user_id=user_id,
                    data=data,
                )
                continue

            if message_type != "SUBSCRIBE":
                logger.warning(
                    "WebSocket unsupported message type: connection_id=%s type=%s",
                    connection_id,
                    message_type,
                )
                await websocket.send_json(
                    {"type": "ERROR", "error": {"message": "Unsupported message type"}}
                )
                continue

            zone_ids = data.get("zoneIds")
            if not isinstance(zone_ids, list) or not all(
                isinstance(item, str) for item in zone_ids
            ):
                logger.warning(
                    "WebSocket invalid SUBSCRIBE payload: connection_id=%s", connection_id
                )
                await websocket.send_json(
                    {
                        "type": "ERROR",
                        "error": {
                            "message": "zoneIds is required and must be a list of strings"
                        },
                    }
                )
                continue

            requested = [str(z).strip() for z in zone_ids if str(z).strip()]
            if is_guest:
                if guest_allowed_zones:
                    requested = [z for z in requested if z in guest_allowed_zones]
                else:
                    requested = []
            subscribed_zones = await ws_manager.subscribe(connection_id, requested)
            await websocket.send_json(
                {"type": "SUBSCRIBED", "data": {"zoneIds": sorted(subscribed_zones)}}
            )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected by client: connection_id=%s", connection_id)
    except Exception:
        logger.exception("WebSocket unexpected error: connection_id=%s", connection_id)
        try:
            await websocket.send_json(
                {"type": "ERROR", "error": {"message": "Internal websocket error"}}
            )
        except Exception:
            pass
    finally:
        disconnected_user = await ws_manager.disconnect(connection_id)
        if (
            disconnected_user
            and ws_manager.count_connections_for_user(disconnected_user) == 0
        ):
            try:
                if disconnected_user.startswith("guest:"):
                    await _publish_guest_ws_presence(
                        disconnected_user[len("guest:") :],
                        online=False,
                    )
                else:
                    from app.services.member_presence_service import publish_member_presence

                    await publish_member_presence(int(disconnected_user), False)
            except Exception:
                logger.exception(
                    "Failed to publish offline presence: user_id=%s",
                    disconnected_user,
                )


@router.websocket("/ws")
async def websocket_handler(websocket: WebSocket) -> None:
    """Authenticate with **`?token=`** member or guest JWT; subscribe zones (see module doc)."""
    await _zone_websocket_session(websocket)


@router.websocket("/ws/messages")
async def websocket_messages_alias(websocket: WebSocket) -> None:
    """Compatibility alias for clients expecting /ws/messages (same handshake as /ws)."""
    await _zone_websocket_session(websocket)
