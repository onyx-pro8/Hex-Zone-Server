"""Device session claim and stale presence handling."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.crud import device as device_crud
from app.database import Base
from app.models.device import Device
from app.schemas.schemas import DeviceCreate
from app.services.device_entitlements import (
    device_presence_is_active,
    expire_stale_device_sessions,
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


def test_release_other_device_sessions_marks_others_offline(test_db):
    owner_id = 1
    first = device_crud.create_device(
        test_db,
        owner_id,
        DeviceCreate(hid="MOB-AAAA1111", name="Parent phone", is_online=True),
    )
    second = device_crud.create_device(
        test_db,
        owner_id,
        DeviceCreate(hid="MOB-BBBB2222", name="Child phone", is_online=True),
    )
    test_db.commit()

    release_other_device_sessions(test_db, owner_id, keep_hid="MOB-BBBB2222")
    test_db.commit()
    test_db.refresh(first)
    test_db.refresh(second)

    assert first.is_online is False
    assert second.is_online is True


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
