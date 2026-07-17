"""Wellness check reverse asks: recipients ask sender; sender batch-replies."""
from datetime import datetime
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.domain.message_types import CanonicalMessageType, MessageCategory, MessageScope
from app.models import Owner, WellnessCheckAcknowledgement, WellnessRecipientAsk, ZoneMessageEvent
from app.models.owner import AccountType, OwnerRole
from app.services import wellness_ack_service as was


@pytest.fixture()
def wellness_db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()


def _owner(db, *, oid: int, email: str) -> Owner:
    owner = Owner(
        id=oid,
        email=email,
        zone_id=f"zone-{oid}",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.ADMINISTRATOR,
        first_name="Test",
        last_name=str(oid),
        hashed_password="x",
        api_key=f"key-{oid}",
        address="addr",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(owner)
    db.flush()
    return owner


def _wellness_event(
    db,
    *,
    event_id: str,
    sender_id: int,
    delivered_owner_ids: list[int],
) -> ZoneMessageEvent:
    event = ZoneMessageEvent(
        id=event_id,
        sender_id=sender_id,
        type=CanonicalMessageType.WELLNESS_CHECK.value,
        category=MessageCategory.ALARM,
        scope=MessageScope.PUBLIC,
        text="Wellness check",
        body_json={},
        zone_id="zone-1",
        metadata_json={
            "delivered_owner_ids": delivered_owner_ids,
            "response_tracking_enabled": True,
            "hid": "HOME-SENSOR-01",
        },
        created_at=datetime.utcnow(),
    )
    db.add(event)
    db.flush()
    return event


def test_recipient_can_ask_sender_and_sender_batch_replies(wellness_db):
    db = wellness_db
    sender = _owner(db, oid=1, email="sender@test.com")
    recipient_a = _owner(db, oid=2, email="a@test.com")
    recipient_b = _owner(db, oid=3, email="b@test.com")
    event_id = str(uuid.uuid4())
    _wellness_event(
        db,
        event_id=event_id,
        sender_id=sender.id,
        delivered_owner_ids=[sender.id, recipient_a.id, recipient_b.id],
    )

    ask_a = was.record_recipient_ask_sender(db, message_event_id=event_id, owner=recipient_a)
    assert ask_a["pending_sender_asks"]
    assert len(ask_a["pending_sender_asks"]) == 1
    assert ask_a["pending_sender_asks"][0]["asker_owner_id"] == recipient_a.id

    ask_b = was.record_recipient_ask_sender(db, message_event_id=event_id, owner=recipient_b)
    assert len(ask_b["pending_sender_asks"]) == 2

    reply = was.record_sender_reply_to_asks(
        db,
        message_event_id=event_id,
        owner=sender,
        status_value="ok",
    )
    assert reply["sender_reply"]["status"] == "ok"
    assert reply["sender_reply"]["answered_asker_ids"] == [recipient_a.id, recipient_b.id]
    assert reply["pending_sender_asks"] == []
    assert len(reply["sender_replies"]) == 1

    summary = was.list_wellness_acknowledgements(db, message_event_id=event_id)
    assert summary["pending_sender_asks"] == []
    assert summary["sender_replies"][0]["answered_asker_ids"] == [
        recipient_a.id,
        recipient_b.id,
    ]


def test_new_ask_after_sender_reply_requires_new_reply(wellness_db):
    db = wellness_db
    sender = _owner(db, oid=1, email="sender@test.com")
    recipient_a = _owner(db, oid=2, email="a@test.com")
    recipient_b = _owner(db, oid=3, email="b@test.com")
    event_id = str(uuid.uuid4())
    _wellness_event(
        db,
        event_id=event_id,
        sender_id=sender.id,
        delivered_owner_ids=[sender.id, recipient_a.id, recipient_b.id],
    )

    was.record_recipient_ask_sender(db, message_event_id=event_id, owner=recipient_a)
    was.record_sender_reply_to_asks(
        db, message_event_id=event_id, owner=sender, status_value="ok"
    )

    ask_b = was.record_recipient_ask_sender(db, message_event_id=event_id, owner=recipient_b)
    assert len(ask_b["pending_sender_asks"]) == 1
    assert ask_b["pending_sender_asks"][0]["asker_owner_id"] == recipient_b.id

    reply_b = was.record_sender_reply_to_asks(
        db, message_event_id=event_id, owner=sender, status_value="need_help"
    )
    assert reply_b["sender_reply"]["status"] == "need_help"
    assert reply_b["sender_reply"]["answered_asker_ids"] == [recipient_b.id]
    assert len(reply_b["sender_replies"]) == 2


def test_duplicate_pending_ask_is_idempotent(wellness_db):
    db = wellness_db
    sender = _owner(db, oid=1, email="sender@test.com")
    recipient = _owner(db, oid=2, email="r@test.com")
    event_id = str(uuid.uuid4())
    _wellness_event(
        db,
        event_id=event_id,
        sender_id=sender.id,
        delivered_owner_ids=[sender.id, recipient.id],
    )

    first = was.record_recipient_ask_sender(db, message_event_id=event_id, owner=recipient)
    second = was.record_recipient_ask_sender(db, message_event_id=event_id, owner=recipient)
    assert first["recipient_ask"]["id"] == second["recipient_ask"]["id"]
    assert len(second["pending_sender_asks"]) == 1
    assert db.query(WellnessRecipientAsk).count() == 1


def test_recipient_ack_and_ask_are_independent(wellness_db):
    db = wellness_db
    sender = _owner(db, oid=1, email="sender@test.com")
    recipient = _owner(db, oid=2, email="r@test.com")
    event_id = str(uuid.uuid4())
    _wellness_event(
        db,
        event_id=event_id,
        sender_id=sender.id,
        delivered_owner_ids=[sender.id, recipient.id],
    )

    ack = was.record_wellness_acknowledgement(
        db, message_event_id=event_id, owner=recipient, status_value="ok"
    )
    assert len(ack["acknowledgements"]) == 1

    ask = was.record_recipient_ask_sender(db, message_event_id=event_id, owner=recipient)
    assert len(ask["pending_sender_asks"]) == 1
    assert db.query(WellnessCheckAcknowledgement).count() == 1


def test_mobile_wellness_check_rejects_responses(wellness_db):
    db = wellness_db
    sender = _owner(db, oid=1, email="sender@test.com")
    recipient = _owner(db, oid=2, email="recipient@test.com")
    event_id = str(uuid.uuid4())
    event = ZoneMessageEvent(
        id=event_id,
        sender_id=sender.id,
        type=CanonicalMessageType.WELLNESS_CHECK.value,
        category=MessageCategory.ALARM,
        scope=MessageScope.PUBLIC,
        text="Wellness check",
        body_json={},
        zone_id="zone-1",
        metadata_json={
            "delivered_owner_ids": [sender.id, recipient.id],
            "response_tracking_enabled": False,
            "hid": "MOB-ABCDEFGH",
        },
        created_at=datetime.utcnow(),
    )
    db.add(event)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        was.record_wellness_acknowledgement(
            db,
            message_event_id=event_id,
            owner=recipient,
            status_value="ok",
        )
    assert exc.value.status_code == 422
