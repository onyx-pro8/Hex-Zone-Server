"""Network access guest arrival and session exchange."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import GuestAccessSession, Owner
from app.models.owner import AccountType, OwnerRole
from app.services import guest_access_service as gas
from app.services import message_feature_service as mfs


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


def test_guest_private_search_requires_coordinates(db):
    network = "NET-ACCESS-3"
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
    search = mfs.search_network_guest_private_message_recipients(
        db,
        guest_session=row,
        query="",
        latitude=None,
        longitude=None,
    )
    assert search["location_status"] == "no_coordinates"
    assert search["members"] == []


def test_network_guest_session_poll_is_auto_approved(db):
    network = "NET-ACCESS-4"
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
    db.commit()
    row = (
        db.query(GuestAccessSession)
        .filter(GuestAccessSession.guest_id == result["guest_response"]["guest_id"])
        .first()
    )
    view = gas.guest_session_public_view(db, row)
    assert view["status"] == "EXPECTED"
    assert view["approval_status"] == "APPROVED"
    assert view.get("exchange_code")
    assert gas._guest_row_client_status(row) == "APPROVED"
