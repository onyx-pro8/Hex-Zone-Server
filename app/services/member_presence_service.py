"""Realtime member online/offline presence over WebSocket."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.database import session_maker
from app.models import Device
from app.services.device_entitlements import (
    device_presence_is_active,
    is_client_session_hid,
    is_smart_home_hid,
)
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


def owner_appears_online(db: Session, owner_id: int) -> bool:
    """Live socket, or a fresh phone/browser session. Hubs do not count."""
    if ws_manager.is_user_connected(owner_id):
        return True
    devices = (
        db.query(Device)
        .filter(Device.owner_id == int(owner_id), Device.is_online.is_(True))
        .all()
    )
    return any(
        is_client_session_hid(device.hid) and device_presence_is_active(device)
        for device in devices
    )


async def broadcast_member_presence(owner_id: int, online: bool) -> None:
    """Fan out MEMBER_PRESENCE without rewriting every device row."""
    await ws_manager.broadcast_to_all(
        "MEMBER_PRESENCE",
        {"owner_id": int(owner_id), "online": bool(online)},
    )


def touch_owner_client_presence(owner_id: int) -> bool:
    """Refresh phone/browser rows so other API workers can see this login.

    Returns True when those rows were not already online, so the caller can
    rebroadcast MEMBER_PRESENCE. A socket lives on one process; another
    worker may have cleared the rows when a different tab disconnected.
    """
    db = session_maker()
    try:
        now = datetime.utcnow()
        devices = db.query(Device).filter(Device.owner_id == int(owner_id)).all()
        was_online = False
        touched = False
        for device in devices:
            if not is_client_session_hid(device.hid):
                continue
            if device.is_online and device_presence_is_active(device):
                was_online = True
            device.is_online = True
            device.last_seen = now
            touched = True
        if touched:
            db.commit()
        return touched and not was_online
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def publish_member_presence(owner_id: int, online: bool) -> None:
    """Fan out MEMBER_PRESENCE and store the same flag on client sessions.

    GET /members reads those rows. Leaving them online after the socket closed
    kept the web badge green for up to 30 minutes while the phone already
    showed the MEMBER_PRESENCE offline event.
    """
    try:
        sync_owner_devices_online(int(owner_id), bool(online))
    except Exception:
        # Presence broadcast should still fire even if device rows fail.
        pass
    await broadcast_member_presence(int(owner_id), bool(online))
