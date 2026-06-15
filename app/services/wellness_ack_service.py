"""Wellness check acknowledgement helpers."""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.domain.message_types import CanonicalMessageType, normalize_message_type
from app.models.owner import Owner
from app.models.wellness_check_acknowledgement import WellnessCheckAcknowledgement
from app.models.zone_message_event import ZoneMessageEvent


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

    return {
        "message_event_id": message_event_id,
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
        "response_tracking_enabled": True,
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

    expected = set(_expected_recipient_ids(event))
    if owner.id not in expected and owner.id != event.sender_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an expected recipient for this wellness check.",
        )

    normalized_status = (status_value or "ok").strip().lower()
    if normalized_status not in {"ok", "need_help"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="status must be 'ok' or 'need_help'.",
        )

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
