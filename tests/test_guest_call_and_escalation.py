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


@pytest.mark.asyncio
async def test_escalated_cancel_and_decline_close_access():
    guest_call, err = await guest_call_service.create_call(
        zone_id="Z-ESC",
        guest_id="guest-esc",
        guest_name="Escalated",
        admin_owner_id=7,
        caller_role="guest",
        fullscreen=True,
        access_reject_escalation=True,
    )
    assert err is None and guest_call is not None
    _ended, payload = await guest_call_service.end_call(
        guest_call.call_id, reason="cancel", closer_role="guest"
    )
    assert payload is not None
    assert payload["auto_close_access"] is True

    admin_call, err2 = await guest_call_service.create_call(
        zone_id="Z-ESC",
        guest_id="guest-esc-2",
        guest_name="Escalated",
        admin_owner_id=7,
        caller_role="guest",
        fullscreen=True,
        access_reject_escalation=True,
    )
    assert err2 is None and admin_call is not None
    _ended2, payload2 = await guest_call_service.end_call(
        admin_call.call_id, reason="reject", closer_role="admin"
    )
    assert payload2 is not None
    assert payload2["auto_close_access"] is True


@pytest.mark.asyncio
async def test_second_call_waits_then_starts_when_first_ends():
    first, queued, err, _promoted = await guest_call_service.enqueue_or_start(
        zone_id="Z-QUEUE",
        guest_id="guest-queue-a",
        guest_name="A",
        admin_owner_id=41,
        caller_role="guest",
    )
    assert err is None and first is not None and queued is None

    second, waiting, err2, _promoted = await guest_call_service.enqueue_or_start(
        zone_id="Z-QUEUE",
        guest_id="guest-queue-b",
        guest_name="B",
        admin_owner_id=41,
        caller_role="guest",
        fullscreen=True,
    )
    assert err2 is None and second is None and waiting is not None
    assert waiting.guest_id == "guest-queue-b"
    assert guest_call_service.queue_position(waiting) == 1

    third, waiting2, err3, _promoted = await guest_call_service.enqueue_or_start(
        zone_id="Z-QUEUE",
        guest_id="guest-queue-c",
        guest_name="C",
        admin_owner_id=41,
        caller_role="admin",
    )
    assert err3 is None and third is None and waiting2 is not None
    assert guest_call_service.queue_position(waiting2) == 2

    await guest_call_service.end_call(first.call_id, reason="hangup", closer_role="admin")
    started = await guest_call_service.promote_ready_queues()
    assert len(started) == 1
    assert started[0].guest_id == "guest-queue-b"
    assert started[0].fullscreen is True
    assert started[0].state == "ringing"

    await guest_call_service.end_call(started[0].call_id, reason="reject", closer_role="admin")
    started2 = await guest_call_service.promote_ready_queues()
    assert len(started2) == 1
    assert started2[0].guest_id == "guest-queue-c"
    await guest_call_service.end_call(started2[0].call_id, reason="cancel", closer_role="admin")
