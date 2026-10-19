"""Network admins can command only their own hub. System admins cannot."""
from datetime import datetime

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Device, Owner
from app.models.owner import AccountType, OwnerRole
from app.services import hub_command_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _owner(db, *, email, account_type, role):
    owner = Owner(
        email=email,
        zone_id="NET",
        first_name="Ada",
        last_name="Lovelace",
        account_type=account_type,
        role=role,
        hashed_password="x",
        api_key=f"key-{email}",
        address="1 Main",
        sn_webhook="https://hub.example/hook",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(owner)
    db.flush()
    owner.account_owner_id = owner.id
    device = Device(
        hid="DEV-HUB1",
        name="Hub",
        owner_id=owner.id,
        address="1 Main",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(device)
    db.commit()
    return owner


class _Response:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class _Client:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, url, json):
        assert json["command"] == "arm"
        return _Response(200, "armed")


def test_network_admin_command_is_recorded(db, monkeypatch):
    admin = _owner(
        db,
        email="net@example.com",
        account_type=AccountType.ENHANCED,
        role=OwnerRole.ADMINISTRATOR,
    )
    monkeypatch.setattr(httpx, "Client", _Client)
    row = hub_command_service.send_hub_command(db, admin, hid="DEV-HUB1", command="arm")
    assert row["status"] == "sent"
    assert row["detail"] == "armed"
    history = hub_command_service.list_hub_commands(db, admin)
    assert history[0]["command"] == "arm"


def test_system_admin_cannot_send_hub_commands(db):
    admin = _owner(
        db,
        email="sys@example.com",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
    )
    with pytest.raises(Exception) as exc:
        hub_command_service.send_hub_command(db, admin, hid="DEV-HUB1", command="status")
    assert getattr(exc.value, "status_code", None) == 403
