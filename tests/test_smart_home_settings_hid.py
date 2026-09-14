"""Settings HID selection among registered smart-home hubs."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Device, Owner
from app.models.owner import AccountType, OwnerRole
from app.routes.contract_routes import (
    SharedNotificationSettingsModel,
    _apply_selected_smart_home_hid,
    _owner_to_settings_model,
    _resolve_selected_smart_home_hid,
    _seed_empty_shared_fields,
    _smart_home_device_for_owner,
    _smart_home_devices_for_owner,
)
from fastapi import HTTPException

TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture
def test_db():
    engine = create_engine(TEST_DATABASE_URL, echo=False)
    testing_session_maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    with testing_session_maker() as session:
        yield session
    Base.metadata.drop_all(bind=engine)


def _owner(db, **kwargs) -> Owner:
    owner = Owner(
        email=kwargs.get("email", "hid-test@example.com"),
        first_name="Hid",
        last_name="Test",
        hashed_password="x",
        address="1 Test St",
        zone_id=kwargs.get("zone_id", "ZONE-HID"),
        api_key=kwargs.get("api_key", "api-key-hid-test"),
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
        active=True,
        sn_hid=kwargs.get("sn_hid", ""),
    )
    db.add(owner)
    db.commit()
    db.refresh(owner)
    return owner


def _device(db, owner_id: int, hid: str, *, name: str | None = None, active: bool = True) -> Device:
    device = Device(
        owner_id=owner_id,
        hid=hid,
        name=name or hid,
        active=active,
        is_online=False,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def test_smart_home_device_prefers_dev_over_phone(test_db):
    owner = _owner(test_db, email="prefer-dev@example.com", api_key="api-prefer-dev")
    _device(test_db, owner.id, "MOB-PHONE001")
    hub = _device(test_db, owner.id, "DEV-HUB0001")

    chosen = _smart_home_device_for_owner(test_db, owner.id)
    assert chosen is not None
    assert chosen.id == hub.id
    assert chosen.hid == "DEV-HUB0001"


def test_smart_home_device_ignores_phone_only(test_db):
    owner = _owner(test_db, email="phone-only@example.com", api_key="api-phone-only")
    _device(test_db, owner.id, "MOB-ONLY0001")

    assert _smart_home_device_for_owner(test_db, owner.id) is None


def test_seed_hid_uses_inactive_hub_not_phone(test_db):
    owner = _owner(test_db, email="inactive-hub@example.com", api_key="api-inactive")
    _device(test_db, owner.id, "MOB-PHONE002")
    _device(test_db, owner.id, "DEV-INACTIVE1", active=False)

    sn = SharedNotificationSettingsModel()
    _seed_empty_shared_fields(sn, owner, test_db)
    assert sn.hid == "DEV-INACTIVE1"
    assert sn.api_key == owner.api_key
    assert sn.network_id == owner.zone_id


def test_seed_hid_respects_saved_selection(test_db):
    owner = _owner(
        test_db,
        email="select-hid@example.com",
        api_key="api-select",
        sn_hid="DEV-SECOND",
    )
    _device(test_db, owner.id, "DEV-FIRST", name="Kitchen")
    _device(test_db, owner.id, "DEV-SECOND", name="Garage")

    hubs = _smart_home_devices_for_owner(test_db, owner.id)
    assert _resolve_selected_smart_home_hid(owner, hubs) == "DEV-SECOND"

    model = _owner_to_settings_model(owner, test_db)
    assert model.shared_notification.hid == "DEV-SECOND"
    assert [d.hid for d in model.smart_home_devices] == ["DEV-FIRST", "DEV-SECOND"]


def test_apply_selected_smart_home_hid_persists(test_db):
    owner = _owner(test_db, email="apply-hid@example.com", api_key="api-apply")
    _device(test_db, owner.id, "DEV-A", name="A")
    _device(test_db, owner.id, "DEV-B", name="B")

    _apply_selected_smart_home_hid(owner, test_db, "DEV-B")
    test_db.commit()
    test_db.refresh(owner)
    assert owner.sn_hid == "DEV-B"

    model = _owner_to_settings_model(owner, test_db)
    assert model.shared_notification.hid == "DEV-B"


def test_apply_selected_smart_home_hid_rejects_phone(test_db):
    owner = _owner(test_db, email="reject-phone@example.com", api_key="api-reject")
    _device(test_db, owner.id, "DEV-OK1")
    _device(test_db, owner.id, "MOB-PHONE003")

    with pytest.raises(HTTPException) as exc:
        _apply_selected_smart_home_hid(owner, test_db, "MOB-PHONE003")
    assert exc.value.status_code == 400


def test_apply_selected_smart_home_hid_rejects_unknown(test_db):
    owner = _owner(test_db, email="reject-unknown@example.com", api_key="api-unknown")
    _device(test_db, owner.id, "DEV-OK2")

    with pytest.raises(HTTPException) as exc:
        _apply_selected_smart_home_hid(owner, test_db, "DEV-NOTMINE")
    assert exc.value.status_code == 400
