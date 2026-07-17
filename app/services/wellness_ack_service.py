"""Wellness check acknowledgement helpers."""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.domain.message_types import CanonicalMessageType, is_smart_home_sender_hid, normalize_message_type
from app.models.owner import Owner
from app.models.wellness_check_acknowledgement import WellnessCheckAcknowledgement
from app.models.wellness_recipient_ask import WellnessRecipientAsk
from app.models.wellness_sender_reply import WellnessSenderReply
from app.models.zone_message_event import ZoneMessageEvent

_VALID_WELLNESS_STATUS = {"ok", "need_help"}


def _expected_recipient_ids(event: ZoneMessageEvent) -> list[int]:
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    delivered = metadata.get("delivered_owner_ids") or []
    sender_id = event.sender_id
    ids: list[int] = []
    for raw in delivered:
        if not isinstance(raw, int):
            continue
        if sender_id is not None and raw == sender_id:
            continue
        ids.append(raw)
    return ids


def _response_tracking_enabled(event: ZoneMessageEvent) -> bool:
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    if "response_tracking_enabled" in metadata:
        return bool(metadata.get("response_tracking_enabled"))
    return is_smart_home_sender_hid(str(metadata.get("hid") or ""))


def _assert_wellness_response_tracking(event: ZoneMessageEvent) -> None:
    if not _response_tracking_enabled(event):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This wellness check does not accept recipient responses.",
        )


def _assert_wellness_event(event: ZoneMessageEvent) -> None:
    try:
        canonical = normalize_message_type(event.type)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Message event is not a wellness check.",
        ) from exc
    if canonical != CanonicalMessageType.WELLNESS_CHECK:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Acknowledgements are only supported for WELLNESS_CHECK messages.",
        )


def _normalize_wellness_status(status_value: str) -> str:
    normalized_status = (status_value or "ok").strip().lower()
    if normalized_status not in _VALID_WELLNESS_STATUS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="status must be 'ok' or 'need_help'.",
        )
    return normalized_status


def _serialize_sender_ask(row: WellnessRecipientAsk) -> dict:
    return {
        "id": row.id,
        "asker_owner_id": row.asker_owner_id,
        "created_at": row.created_at.isoformat(),
        "sender_reply_id": row.sender_reply_id,
    }


def _serialize_sender_reply(
    row: WellnessSenderReply,
    *,
    answered_asker_ids: list[int],
) -> dict:
    return {
        "id": row.id,
        "status": row.status,
        "note": row.note,
        "created_at": row.created_at.isoformat(),
        "answered_asker_ids": answered_asker_ids,
    }


def _load_sender_ask_state(db: Session, *, message_event_id: str) -> tuple[list[dict], list[dict]]:
    ask_rows = (
        db.query(WellnessRecipientAsk)
        .filter(WellnessRecipientAsk.message_event_id == message_event_id)
        .order_by(WellnessRecipientAsk.created_at.asc())
        .all()
    )
    reply_rows = (
        db.query(WellnessSenderReply)
        .filter(WellnessSenderReply.message_event_id == message_event_id)
        .order_by(WellnessSenderReply.created_at.asc())
        .all()
    )
    reply_askers: dict[str, list[int]] = {}
    for ask in ask_rows:
        if ask.sender_reply_id:
            reply_askers.setdefault(ask.sender_reply_id, []).append(ask.asker_owner_id)

    pending_sender_asks = [
        _serialize_sender_ask(row) for row in ask_rows if row.sender_reply_id is None
    ]
    sender_replies = [
        _serialize_sender_reply(
            row,
            answered_asker_ids=sorted(reply_askers.get(row.id, [])),
        )
        for row in reply_rows
    ]
    return pending_sender_asks, sender_replies


def list_wellness_acknowledgements(db: Session, *, message_event_id: str) -> dict:
    event = db.get(ZoneMessageEvent, message_event_id)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    _assert_wellness_event(event)

    rows = (
        db.query(WellnessCheckAcknowledgement)
        .filter(WellnessCheckAcknowledgement.message_event_id == message_event_id)
        .order_by(WellnessCheckAcknowledgement.created_at.asc())
        .all()
    )
    expected = _expected_recipient_ids(event)
    ack_owner_ids = {row.owner_id for row in rows}
    pending = [oid for oid in expected if oid not in ack_owner_ids]
    pending_sender_asks, sender_replies = _load_sender_ask_state(
        db, message_event_id=message_event_id
    )

    return {
        "message_event_id": message_event_id,
        "sender_id": event.sender_id,
        "expected_recipient_ids": expected,
        "pending_recipient_ids": pending,
        "acknowledgements": [
            {
                "id": row.id,
                "owner_id": row.owner_id,
                "status": row.status,
                "note": row.note,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
        "pending_sender_asks": pending_sender_asks,
        "sender_replies": sender_replies,
        "response_tracking_enabled": _response_tracking_enabled(event),
    }


def record_wellness_acknowledgement(
    db: Session,
    *,
    message_event_id: str,
    owner: Owner,
    status_value: str = "ok",
    note: str | None = None,
) -> dict:
    event = db.get(ZoneMessageEvent, message_event_id)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    _assert_wellness_event(event)
    _assert_wellness_response_tracking(event)

    expected = set(_expected_recipient_ids(event))
    if owner.id not in expected and owner.id != event.sender_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an expected recipient for this wellness check.",
        )

    normalized_status = _normalize_wellness_status(status_value)

    existing = (
        db.query(WellnessCheckAcknowledgement)
        .filter(
            WellnessCheckAcknowledgement.message_event_id == message_event_id,
            WellnessCheckAcknowledgement.owner_id == owner.id,
        )
        .first()
    )
    if existing:
        existing.status = normalized_status
        existing.note = note
        existing.created_at = datetime.utcnow()
        row = existing
    else:
        row = WellnessCheckAcknowledgement(
            message_event_id=message_event_id,
            owner_id=owner.id,
            status=normalized_status,
            note=note,
        )
        db.add(row)
    db.flush()

    summary = list_wellness_acknowledgements(db, message_event_id=message_event_id)
    return {
        "acknowledgement": {
            "id": row.id,
            "owner_id": row.owner_id,
            "status": row.status,
            "note": row.note,
            "created_at": row.created_at.isoformat(),
        },
        **summary,
    }


def record_recipient_ask_sender(
    db: Session,
    *,
    message_event_id: str,
    owner: Owner,
) -> dict:
    event = db.get(ZoneMessageEvent, message_event_id)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    _assert_wellness_event(event)
    _assert_wellness_response_tracking(event)

    expected = set(_expected_recipient_ids(event))
    if owner.id not in expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only wellness check recipients can ask the sender to respond.",
        )

    existing_pending = (
        db.query(WellnessRecipientAsk)
        .filter(
            WellnessRecipientAsk.message_event_id == message_event_id,
            WellnessRecipientAsk.asker_owner_id == owner.id,
            WellnessRecipientAsk.sender_reply_id.is_(None),
        )
        .first()
    )
    if existing_pending:
        row = existing_pending
    else:
        row = WellnessRecipientAsk(
            message_event_id=message_event_id,
            asker_owner_id=owner.id,
        )
        db.add(row)
        db.flush()

    summary = list_wellness_acknowledgements(db, message_event_id=message_event_id)
    return {
        "recipient_ask": _serialize_sender_ask(row),
        **summary,
    }


def record_sender_reply_to_asks(
    db: Session,
    *,
    message_event_id: str,
    owner: Owner,
    status_value: str = "ok",
    note: str | None = None,
) -> dict:
    event = db.get(ZoneMessageEvent, message_event_id)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    _assert_wellness_event(event)
    _assert_wellness_response_tracking(event)

    if event.sender_id is None or owner.id != event.sender_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the wellness check sender can reply to recipient asks.",
        )

    pending_asks = (
        db.query(WellnessRecipientAsk)
        .filter(
            WellnessRecipientAsk.message_event_id == message_event_id,
            WellnessRecipientAsk.sender_reply_id.is_(None),
        )
        .order_by(WellnessRecipientAsk.created_at.asc())
        .all()
    )
    if not pending_asks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No pending recipient asks for this wellness check.",
        )

    normalized_status = _normalize_wellness_status(status_value)
    reply = WellnessSenderReply(
        message_event_id=message_event_id,
        status=normalized_status,
        note=note,
    )
    db.add(reply)
    db.flush()

    answered_asker_ids: list[int] = []
    for ask in pending_asks:
        ask.sender_reply_id = reply.id
        answered_asker_ids.append(ask.asker_owner_id)
    db.flush()

    summary = list_wellness_acknowledgements(db, message_event_id=message_event_id)
    return {
        "sender_reply": _serialize_sender_reply(
            reply,
            answered_asker_ids=sorted(answered_asker_ids),
        ),
        **summary,
    }


def wellness_ack_notify_owner_ids(db: Session, *, message_event_id: str) -> list[int]:
    """Owner ids that should receive a realtime wellness summary refresh."""
    event = db.get(ZoneMessageEvent, message_event_id)
    if not event:
        return []
    ids = set(_expected_recipient_ids(event))
    if event.sender_id is not None:
        ids.add(int(event.sender_id))
    return sorted(ids)
