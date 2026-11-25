"""Device session claim and stale presence handling."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.crud import device as device_crud
from app.database import Base
from app.models.device import Device
from app.models.owner import AccountType, Owner, OwnerRole
from app.schemas.schemas import DeviceCreate
from app.services.device_entitlements import (
    device_presence_is_active,
    evict_offline_devices_to_make_room,
    expire_stale_device_sessions,
    is_client_session_hid,
    is_smart_home_hid,
    release_other_device_sessions,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture
def test_db():
    engine = create_engine(TEST_DATABASE_URL, echo=False)
    testing_session_maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    with testing_session_maker() as session:
        yield session
    Base.metadata.drop_all(bind=engine)


def test_stale_online_device_is_not_active():
    device = Device(
        hid="MOB-STALE01",
        name="Stale phone",
        owner_id=1,
        is_online=True,
        last_seen=datetime.utcnow() - timedelta(hours=2),
    )
    assert device_presence_is_active(device) is False


def test_release_other_device_sessions_deletes_other_login_clients(test_db):
    owner_id = 1
    first = device_crud.create_device(
        test_db,
        owner_id,
        DeviceCreate(hid="MOB-AAAA1111", name="Parent phone", is_online=True),
    )
    first_id = first.id
    second = device_crud.create_device(
        test_db,
        owner_id,
        DeviceCreate(hid="MOB-BBBB2222", name="Child phone", is_online=True),
    )
    hub = device_crud.create_device(
        test_db,
        owner_id,
        DeviceCreate(hid="DEV-HUB001", name="Smart home", is_online=True),
    )
    test_db.commit()

    released = release_other_device_sessions(
        test_db, owner_id, keep_hid="MOB-BBBB2222"
    )
    test_db.commit()
    test_db.refresh(second)
    test_db.refresh(hub)

    assert "MOB-AAAA1111" in released
    assert device_crud.get_device(test_db, first_id, owner_id=owner_id) is None
    assert second.is_online is True
    assert hub.is_online is True
    assert is_smart_home_hid(hub.hid) is True
    assert is_client_session_hid(second.hid) is True


def test_release_other_device_sessions_removes_offline_login_clients(test_db):
    owner_id = 1
    offline = device_crud.create_device(
        test_db,
        owner_id,
        DeviceCreate(hid="WEB-OLD00001", name="Old browser", is_online=False),
    )
    offline_id = offline.id
    keep = device_crud.create_device(
        test_db,
        owner_id,
        DeviceCreate(hid="MOB-KEEP9999", name="New phone", is_online=True),
    )
    test_db.commit()

    released = release_other_device_sessions(
        test_db, owner_id, keep_hid="MOB-KEEP9999"
    )
    test_db.commit()
    test_db.refresh(keep)

    assert "WEB-OLD00001" in released
    assert device_crud.get_device(test_db, offline_id, owner_id=owner_id) is None
    assert keep.is_online is True


def test_expire_stale_device_sessions(test_db):
    owner_id = 1
    stale = device_crud.create_device(
        test_db,
        owner_id,
        DeviceCreate(hid="MOB-STALE222", name="Old phone", is_online=True),
    )
    stale.last_seen = datetime.utcnow() - timedelta(hours=3)
    fresh = device_crud.create_device(
        test_db,
        owner_id,
        DeviceCreate(hid="MOB-FRESH333", name="Fresh phone", is_online=True),
    )
    fresh.last_seen = datetime.utcnow()
    test_db.commit()

    expire_stale_device_sessions(test_db, owner_id)
    test_db.commit()
    test_db.refresh(stale)
    test_db.refresh(fresh)

    assert stale.is_online is False
    assert fresh.is_online is True


def test_zero_hub_cap_ignores_login_sessions(test_db):
    """Individual accounts can switch WEB-/MOB- sessions with no hubs registered."""
    owner = Owner(
        email="individual@example.com",
        username="individual1",
        zone_id="individual-zone",
        first_name="Indi",
        last_name="Vidual",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
        hashed_password="hashed",
        api_key="individual-api-key",
        address="1 Main St",
    )
    test_db.add(owner)
    test_db.commit()
    test_db.refresh(owner)

    web = device_crud.create_device(
        test_db,
        owner.id,
        DeviceCreate(hid="WEB-MARKET1", name="Marketing", is_online=True),
    )
    phone = device_crud.create_device(
        test_db,
        owner.id,
        DeviceCreate(hid="MOB-PHONE001", name="Phone", is_online=False),
    )
    test_db.commit()

    evict_offline_devices_to_make_room(test_db, owner)
    test_db.commit()

    assert device_crud.get_device(test_db, web.id, owner_id=owner.id) is not None
    assert device_crud.get_device(test_db, phone.id, owner_id=owner.id) is not None
