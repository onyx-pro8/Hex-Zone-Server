"""CRUD operations for Device."""
from datetime import datetime
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.future import select
from sqlalchemy import func
from app.models import Device
from app.schemas.schemas import DeviceCreate, DeviceUpdate
from app.core.h3_utils import lat_lng_to_h3_cell
from typing import Optional, List


def create_device(db: Session, owner_id: int, device: DeviceCreate) -> Device:
    """Create a new device."""
    h3_cell_id = None
    if device.latitude is not None and device.longitude is not None:
        h3_cell_id = lat_lng_to_h3_cell(device.latitude, device.longitude)
    
    db_device = Device(
        hid=device.hid,
        name=device.name,
        latitude=device.latitude,
        longitude=device.longitude,
        address=device.address,
        h3_cell_id=h3_cell_id,
        owner_id=owner_id,
        propagate_enabled=device.propagate_enabled,
        propagate_radius_km=device.propagate_radius_km,
        active=device.active if device.active is not None else True,
        is_online=device.is_online if device.is_online is not None else False,
        enable_notification=device.enable_notification,
        alert_threshold_meters=device.alert_threshold_meters,
        update_interval_seconds=device.update_interval_seconds,
        webhook=(device.webhook or "").strip(),
        periodical_check_sec=(device.periodical_check_sec or "86400").strip() or "86400",
        zone_ids=list(device.zone_ids or []),
    )
    db.add(db_device)
    db.flush()
    db.refresh(db_device)
    return db_device


def get_device(
    db: Session,
    device_id: int,
    owner_id: Optional[int] = None,
    owner_ids: Optional[List[int]] = None,
    load_owner: bool = False,
) -> Optional[Device]:
    """Get a device by ID, optionally filtered by owner."""
    query = select(Device).where(Device.id == device_id)
    if load_owner:
        query = query.options(joinedload(Device.owner))
    if owner_id is not None:
        query = query.where(Device.owner_id == owner_id)
    if owner_ids is not None:
        query = query.where(Device.owner_id.in_(owner_ids))
    result = db.execute(query)
    return result.scalars().first()


def get_device_by_hid(
    db: Session,
    hid: str,
    owner_id: Optional[int] = None,
    owner_ids: Optional[List[int]] = None,
    load_owner: bool = False,
) -> Optional[Device]:
    """Get a device by hardware ID."""
    query = select(Device).where(Device.hid == hid)
    if load_owner:
        query = query.options(joinedload(Device.owner))
    if owner_id is not None:
        query = query.where(Device.owner_id == owner_id)
    if owner_ids is not None:
        query = query.where(Device.owner_id.in_(owner_ids))
    result = db.execute(query)
    return result.scalars().first()


def list_devices(
    db: Session,
    owner_id: Optional[int] = None,
    owner_ids: Optional[List[int]] = None,
    skip: int = 0,
    limit: Optional[int] = None,
    active_only: bool = False,
    load_owner: bool = False,
) -> List[Device]:
    """List devices for an owner."""
    query = select(Device)
    if load_owner:
        query = query.options(joinedload(Device.owner))
    if owner_id is not None:
        query = query.where(Device.owner_id == owner_id)
    if owner_ids is not None:
        query = query.where(Device.owner_id.in_(owner_ids))
    if active_only:
        query = query.where(Device.active == True)
    query = query.offset(skip)
    if limit is not None:
        query = query.limit(limit)
    result = db.execute(query)
    return result.scalars().all()


def update_device(
    db: Session,
    device_id: int,
    device_update: DeviceUpdate,
    owner_id: Optional[int] = None,
    owner_ids: Optional[List[int]] = None,
) -> Optional[Device]:
    """Update a device."""
    db_device = get_device(db, device_id, owner_id=owner_id, owner_ids=owner_ids)
    if not db_device:
        return None
    
    update_data = device_update.model_dump(exclude_unset=True, exclude={"status"})
    for field, value in update_data.items():
        setattr(db_device, field, value)

    if db_device.latitude is not None and db_device.longitude is not None:
        db_device.h3_cell_id = lat_lng_to_h3_cell(db_device.latitude, db_device.longitude)

    db.flush()
    db.refresh(db_device)
    return db_device


def touch_presence(db: Session, db_device: Device) -> None:
    """Mark device as online and refresh last_seen (UTC)."""
    db_device.last_seen = datetime.utcnow()
    db_device.is_online = True
    db.flush()


def delete_device(
    db: Session,
    device_id: int,
    owner_id: Optional[int] = None,
    owner_ids: Optional[List[int]] = None,
) -> bool:
    """Delete a device."""
    db_device = get_device(db, device_id, owner_id=owner_id, owner_ids=owner_ids)
    if not db_device:
        return False
    
    db.delete(db_device)
    return True


def count_devices(db: Session, owner_id: int) -> int:
    """Count devices for an owner."""
    result = db.execute(
        select(func.count(Device.id)).where(Device.owner_id == owner_id)
    )
    return result.scalar()
