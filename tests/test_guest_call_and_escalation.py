"""Guest access-reject escalation and call session helpers."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import GuestAccessSession, Owner
from app.models.owner import AccountType, OwnerRole
from app.services import guest_access_service as gas
from app.services import guest_call_service


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
        email="admin-call@test.com",
        zone_id=network_id,
        first_name="A",
        last_name="Admin",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        hashed_password="x",
        api_key="key-admin-call",
        address="addr",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(owner)
    db.flush()
    return owner


def test_normalize_guest_name_key_casefold():
    assert gas.normalize_guest_name_key("  Sam ") == gas.normalize_guest_name_key("sam")


def test_count_access_rejects_for_guest_name(db):
    network = "NET-ESCALATE-1"
    _admin(db, network)
    for i in range(3):
        db.add(
            GuestAccessSession(
                guest_id=f"g-reject-{i}",
                zone_id=network,
                guest_name="Sam Guest",
                kind="unexpected",
                resolution="rejected",
                created_at=datetime.utcnow(),
            )
        )
    db.flush()
    assert (
        gas.count_access_rejects_for_guest_name(
            db, zone_id=network, guest_name="sam guest"
        )
        == 3
    )
    assert gas.access_reject_escalation_active(
        db, zone_id=network, guest_name="SAM GUEST"
    )


def test_fourth_arrival_sets_fullscreen_auto_call(db):
    network = "NET-ESCALATE-2"
    _admin(db, network)
    for i in range(3):
        db.add(
            GuestAccessSession(
                guest_id=f"g-prior-{i}",
                zone_id=network,
                guest_name="Sam Guest",
                kind="network_access",
                resolution="rejected",
                created_at=datetime.utcnow(),
            )
        )
    db.flush()
    result = gas.process_network_guest_arrival(
        db,
        network_id=network,
        guest_name="sam guest",
        device_id="dev",
        latitude=None,
        longitude=None,
        qr_token_db_id=None,
    )
    assert "error" not in result
    assert result["guest_response"].get("call_fullscreen") is True
    assert result.get("auto_call_invite", {}).get("fullscreen") is True
    assert result["auto_call_invite"].get("caller_role") == "guest"
    assert result["auto_call_invite"].get("access_reject_escalation") is True


@pytest.mark.asyncio
async def test_create_call_blocks_second_active():
    call, err = await guest_call_service.create_call(
        zone_id="Z1",
        guest_id="guest-a",
        guest_name="A",
        admin_owner_id=9,
        caller_role="admin",
    )
    assert err is None and call is not None
    call2, err2 = await guest_call_service.create_call(
        zone_id="Z1",
        guest_id="guest-a",
        guest_name="A",
        admin_owner_id=9,
        caller_role="guest",
    )
    assert call2 is None
    assert err2 == "CALL_ALREADY_ACTIVE"
    await guest_call_service.end_call(
        call.call_id, reason="hangup", closer_role="admin"
    )
