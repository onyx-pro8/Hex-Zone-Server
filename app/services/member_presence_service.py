"""Realtime member online/offline presence over WebSocket."""
from __future__ import annotations

from datetime import datetime

from app.database import session_maker
from app.models import Device
from app.services.device_entitlements import is_smart_home_hid
from app.websocket.manager import ws_manager


def sync_owner_devices_online(owner_id: int, online: bool) -> None:
    """Keep Device.is_online aligned with live WebSocket presence."""
    db = session_maker()
    try:
        devices = db.query(Device).filter(Device.owner_id == int(owner_id)).all()
        now = datetime.utcnow()
        for device in devices:
            if is_smart_home_hid(device.hid):
                continue
            device.is_online = bool(online)
            if online:
                device.last_seen = now
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def publish_member_presence(owner_id: int, online: bool) -> None:
    """Persist device flags and fan out MEMBER_PRESENCE to all connected clients."""
    try:
        sync_owner_devices_online(int(owner_id), bool(online))
    except Exception:
        # Presence broadcast should still fire even if device rows fail.
        pass
    await ws_manager.broadcast_to_all(
        "MEMBER_PRESENCE",
        {"owner_id": int(owner_id), "online": bool(online)},
    )
