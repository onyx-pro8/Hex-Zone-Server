"""Network access guest arrival and session exchange."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import GuestAccessSession, Owner
from app.models.guest_pass import GuestPass, GuestPassStatus
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


def test_process_network_guest_arrival_requires_approval(db):
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
    assert gr["status"] == "UNEXPECTED"
    assert gr["zone_id"] == network
    assert "exchange_code" not in gr
    assert result["ws_unexpected_guest"]


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
    assert not gas.session_allows_network_geo_messaging(row)


def test_guest_private_search_requires_coordinates(db):
    network = "NET-ACCESS-3"
    admin = _admin(db, network)
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
    approved = gas.approve_guest(db, acting_owner=admin, zone_id=network, guest_id=row.guest_id)
    assert approved.get("ok")
    db.commit()
    search = mfs.search_network_guest_private_message_recipients(
        db,
        guest_session=row,
        query="",
        latitude=None,
        longitude=None,
    )
    assert search["location_status"] == "no_coordinates"
    assert search["members"] == []


def test_network_guest_session_poll_pending_until_approved(db):
    network = "NET-ACCESS-4"
    admin = _admin(db, network)
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
    assert view["status"] == "UNEXPECTED"
    assert view["approval_status"] == "PENDING"
    assert "exchange_code" not in view
    assert gas._guest_row_client_status(row) == "PENDING"

    approved = gas.approve_guest(db, acting_owner=admin, zone_id=network, guest_id=row.guest_id)
    assert approved.get("ok")
    db.commit()
    db.refresh(row)

    view_after = gas.guest_session_public_view(db, row)
    assert view_after["approval_status"] == "APPROVED"
    assert view_after.get("exchange_code")
    assert gas._guest_row_client_status(row) == "APPROVED"


def test_accepted_guest_pass_auto_approves_network_arrival(db):
    network = "NET-GP-1"
    admin = _admin(db, network)
    event_id = "EVT-2026-73"
    guest_pass = GuestPass(
        zone_id=network,
        event_id=event_id,
        requested_by=admin.id,
        reviewed_by=admin.id,
        guest_name="Pass Guest",
        status=GuestPassStatus.ACCEPTED,
        expires_at=datetime.utcnow() + timedelta(days=1),
    )
    db.add(guest_pass)
    db.commit()

    result = gas.process_guest_arrival(
        db,
        zone_id=network,
        guest_name="Walk-in",
        event_id=event_id,
        device_id="dev-gp",
        latitude=40.0,
        longitude=-74.0,
        qr_token_db_id=None,
    )
    assert "error" not in result
    gr = result["guest_response"]
    assert gr["status"] == "EXPECTED"
    assert gr.get("exchange_code")
    db.commit()
    row = (
        db.query(GuestAccessSession)
        .filter(GuestAccessSession.guest_id == gr["guest_id"])
        .first()
    )
    assert row is not None
    assert row.kind == "expected"
    assert gas._guest_row_client_status(row) == "APPROVED"
    db.refresh(guest_pass)
    assert guest_pass.used_by_guest_id == gr["guest_id"]
