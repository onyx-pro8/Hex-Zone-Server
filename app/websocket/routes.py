"""Websocket endpoint for realtime zone subscriptions.

After **`?token=`** JWT auth, clients send JSON text frames:

- **`type=SUBSCRIBE`** with **`zoneIds`** array — zone fan-out for messages.
- **`type=LOCATION_UPDATE`** with **`latitude`/`longitude`** (top-level or under **`data`**) —
  upserts live GPS via the same path as **`POST /message-feature/members/location`**.

Server sends **`type`** + **`data`** envelopes. Common **`type`** values:

- **`NEW_MESSAGE`** — **`data`** matches **`ZoneMessageResponse`** JSON.
- **`NEW_GEO_MESSAGE`**, **`PERMISSION_MESSAGE`**, **`WELLNESS_ACK`**
- **`guest_is_here`**, **`unexpected_guest`**, **`GUEST_REQUEST_CHANGED`**
- **`BLOCKS_CHANGED`** — block-rule create/delete for the owning user
- **`SESSION_REVOKED`** — another device claimed the account session
- **`MEMBER_PRESENCE`** — `{ owner_id, online }` when a member connects/disconnects
- **`LOCATION_UPDATE_ACK`** — ack after a successful **`LOCATION_UPDATE`**
- **`guest_zone_message`** — legacy **`POST /api/guest/messages`** push
- **`GUEST_PRESENCE`** — `{ guest_id, online }` when a guest hits `/api/guest/*` or sends Access CHAT
"""
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi import HTTPException

from app.core.security import verify_token
from app.crud import owner as owner_crud
from app.database import session_maker
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
    user_id = str(payload.get("sub"))
    if not user_id or user_id == "None":
        logger.warning("WebSocket auth failed: invalid subject in token")
        await websocket.close(code=1008, reason="Invalid token")
        return

    logger.info("WebSocket auth succeeded: user_id=%s", user_id)
    connection_id = await ws_manager.connect(user_id, websocket)
    if ws_manager.count_connections_for_user(user_id) == 1:
        try:
            from app.services.member_presence_service import publish_member_presence

            await publish_member_presence(int(user_id), True)
        except Exception:
            logger.exception("Failed to publish online presence: user_id=%s", user_id)
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
                await _handle_location_update(websocket, user_id, data)
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
            if not isinstance(zone_ids, list) or not all(isinstance(item, str) for item in zone_ids):
                logger.warning("WebSocket invalid SUBSCRIBE payload: connection_id=%s", connection_id)
                await websocket.send_json(
                    {
                        "type": "ERROR",
                        "error": {"message": "zoneIds is required and must be a list of strings"},
                    }
                )
                continue

            subscribed_zones = await ws_manager.subscribe(connection_id, zone_ids)
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
                from app.services.member_presence_service import publish_member_presence

                await publish_member_presence(int(disconnected_user), False)
            except Exception:
                logger.exception(
                    "Failed to publish offline presence: user_id=%s",
                    disconnected_user,
                )


@router.websocket("/ws")
async def websocket_handler(websocket: WebSocket) -> None:
    """Authenticate with **`?token=`** bearer JWT (**`sub`** = **`owners.id`** string); subscribe zones (see module doc **`NEW_MESSAGE`**)."""
    await _zone_websocket_session(websocket)


@router.websocket("/ws/messages")
async def websocket_messages_alias(websocket: WebSocket) -> None:
    """Compatibility alias for clients expecting /ws/messages (same handshake as /ws)."""
    await _zone_websocket_session(websocket)
