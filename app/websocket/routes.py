"""Websocket endpoint for realtime zone subscriptions.

After **`?token=`** JWT auth, clients send JSON text **`type=SUBSCRIBE`** with **`zoneIds`** array.

Server sends **`type`** + **`data`** envelopes. Common **`type`** values:

- **`NEW_MESSAGE`** — **`data`** matches **`ZoneMessageResponse`** JSON for member posts or (when **`MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT`**) Access **`CHAT`** (**UUID **`id`**, **`guest_id`** when present) delivered to participant **`owners.id`**.
- **`guest_zone_message`** — legacy **`POST /api/guest/messages`** push (nested **`event`** in **`data`**).
"""
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi import HTTPException

from app.core.security import verify_token
from app.websocket.manager import ws_manager

router = APIRouter()
logger = logging.getLogger(__name__)


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
        await ws_manager.disconnect(connection_id)


@router.websocket("/ws")
async def websocket_handler(websocket: WebSocket) -> None:
    """Authenticate with **`?token=`** bearer JWT (**`sub`** = **`owners.id`** string); subscribe zones (see module doc **`NEW_MESSAGE`**)."""
    await _zone_websocket_session(websocket)


@router.websocket("/ws/messages")
async def websocket_messages_alias(websocket: WebSocket) -> None:
    """Compatibility alias for clients expecting /ws/messages (same handshake as /ws)."""
    await _zone_websocket_session(websocket)
