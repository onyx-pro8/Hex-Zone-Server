"""In-memory websocket connection and subscription manager."""
import asyncio
import logging
from dataclasses import dataclass, field
from uuid import uuid4

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class ConnectionState:
    connection_id: str
    user_id: str
    websocket: WebSocket
    zone_ids: set[str] = field(default_factory=set)


class WebSocketManager:
    def __init__(self) -> None:
        self._connections: dict[str, ConnectionState] = {}
        self._zone_subscribers: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, websocket: WebSocket) -> str:
        connection_id = uuid4().hex
        await websocket.accept()
        async with self._lock:
            self._connections[connection_id] = ConnectionState(
                connection_id=connection_id,
                user_id=str(user_id),
                websocket=websocket,
            )
        logger.info("WebSocket connected: connection_id=%s user_id=%s", connection_id, user_id)
        return connection_id

    async def disconnect(self, connection_id: str) -> str | None:
        async with self._lock:
            state = self._connections.pop(connection_id, None)
            if not state:
                return None
            for zone_id in state.zone_ids:
                subscribers = self._zone_subscribers.get(zone_id)
                if not subscribers:
                    continue
                subscribers.discard(connection_id)
                if not subscribers:
                    self._zone_subscribers.pop(zone_id, None)
            user_id = state.user_id
        logger.info("WebSocket disconnected: connection_id=%s", connection_id)
        return user_id

    def is_user_connected(self, user_id: str | int) -> bool:
        """Best-effort presence check (no lock; fine for display)."""
        uid = str(user_id)
        return any(state.user_id == uid for state in self._connections.values())

    def count_connections_for_user(self, user_id: str | int) -> int:
        uid = str(user_id)
        return sum(1 for state in self._connections.values() if state.user_id == uid)

    async def subscribe(self, connection_id: str, zone_ids: list[str]) -> set[str]:
        normalized = {str(zone_id).strip() for zone_id in zone_ids if str(zone_id).strip()}
        async with self._lock:
            state = self._connections.get(connection_id)
            if not state:
                return set()

            for old_zone_id in state.zone_ids:
                subscribers = self._zone_subscribers.get(old_zone_id)
                if not subscribers:
                    continue
                subscribers.discard(connection_id)
                if not subscribers:
                    self._zone_subscribers.pop(old_zone_id, None)

            state.zone_ids = normalized
            for zone_id in normalized:
                self._zone_subscribers.setdefault(zone_id, set()).add(connection_id)
            user_id = state.user_id

        logger.info(
            "WebSocket subscription updated: connection_id=%s user_id=%s zones=%s",
            connection_id,
            user_id,
            sorted(normalized),
        )
        return normalized

    async def broadcast_to_all(self, event_type: str, payload: dict) -> None:
        recipients = await self._snapshot_connections()
        await self._broadcast(recipients, event_type, payload)

    async def broadcast_message(self, zone_id: str, payload: dict) -> None:
        async with self._lock:
            connection_ids = list(self._zone_subscribers.get(str(zone_id), set()))
            recipients = [
                self._connections[connection_id]
                for connection_id in connection_ids
                if connection_id in self._connections
            ]
        await self._broadcast(recipients, "NEW_MESSAGE", payload)

    async def broadcast_to_users(
        self, user_ids: list[int | str], event_type: str, payload: dict
    ) -> None:
        user_id_set = {str(item) for item in user_ids}
        async with self._lock:
            recipients = [
                state
                for state in self._connections.values()
                if state.user_id in user_id_set
            ]
        await self._broadcast(recipients, event_type, payload)

    async def _snapshot_connections(self) -> list[ConnectionState]:
        async with self._lock:
            return list(self._connections.values())

    async def _broadcast(self, recipients: list[ConnectionState], event_type: str, payload: dict) -> None:
        dead_connection_ids: list[str] = []
        message = {"type": event_type, "data": payload}
        for state in recipients:
            try:
                await state.websocket.send_json(message)
            except Exception:
                logger.exception("Failed sending websocket message: connection_id=%s", state.connection_id)
                dead_connection_ids.append(state.connection_id)

        for connection_id in dead_connection_ids:
            await self.disconnect(connection_id)

    async def get_connection_count(self) -> int:
        async with self._lock:
            return len(self._connections)


ws_manager = WebSocketManager()
