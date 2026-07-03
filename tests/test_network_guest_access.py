"""Network access guest arrival and session exchange."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import GuestAccessSession, Owner
from app.models.owner import AccountType, OwnerRole
from app.services import guest_access_service as gas


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


def _admin(db, network_id: str) -> Owner:
    owner = Owner(
        email="admin@test.com",
        zone_id=network_id,
        first_name="A",
        last_name="Admin",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        hashed_password="x",
        api_key="key-admin",
        address="addr",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(owner)
    db.flush()
    return owner


def test_process_network_guest_arrival_auto_expected(db):
    network = "NET-ACCESS-1"
    _admin(db, network)
    result = gas.process_network_guest_arrival(
        db,
        network_id=network,
        guest_name="Walk-in",
        device_id="dev-1",
        latitude=40.0,
        longitude=-74.0,
        qr_token_db_id=None,
    )
    assert "error" not in result
    gr = result["guest_response"]
    assert gr["status"] == "EXPECTED"
    assert gr["zone_id"] == network
    assert gr["exchange_code"]


def test_session_allows_network_geo_messaging(db):
    network = "NET-ACCESS-2"
    _admin(db, network)
    result = gas.process_network_guest_arrival(
        db,
        network_id=network,
        guest_name="Visitor",
        device_id=None,
        latitude=None,
        longitude=None,
        qr_token_db_id=None,
    )
    db.commit()
    row = (
        db.query(GuestAccessSession)
        .filter(GuestAccessSession.guest_id == result["guest_response"]["guest_id"])
        .first()
    )
    assert row is not None
    assert gas.session_allows_network_geo_messaging(row)
