"""Alarm read receipts for zone message events."""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.domain.message_types import MessageCategory
from app.models.alarm_message_read import AlarmMessageRead
from app.models.owner import Owner
from app.models.zone_message_event import ZoneMessageEvent
from app.schemas.schemas import ZoneMessageResponse


def _event_visible_to_owner(event: ZoneMessageEvent, owner_id: int) -> bool:
    if event.sender_id == owner_id or event.receiver_id == owner_id:
        return True
    meta = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    delivered = meta.get("delivered_owner_ids")
    return isinstance(delivered, list) and owner_id in delivered


def _assert_alarm_event(event: ZoneMessageEvent) -> None:
    if event.category != MessageCategory.ALARM:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Read receipts are only supported for alarm-category messages.",
        )


def read_owner_ids_by_message_ids(
    db: Session,
    message_event_ids: list[str],
) -> dict[str, list[int]]:
    if not message_event_ids:
        return {}
    rows = (
        db.query(AlarmMessageRead.message_event_id, AlarmMessageRead.owner_id)
        .filter(AlarmMessageRead.message_event_id.in_(message_event_ids))
        .order_by(AlarmMessageRead.created_at.asc())
        .all()
    )
    grouped: dict[str, list[int]] = {}
    for message_event_id, owner_id in rows:
        grouped.setdefault(str(message_event_id), []).append(int(owner_id))
    return grouped


def enrich_responses_with_alarm_reads(
    responses: list[ZoneMessageResponse],
    viewer_id: int,
    db: Session,
) -> list[ZoneMessageResponse]:
    alarm_ids = [
        str(item.id)
        for item in responses
        if item.category == MessageCategory.ALARM.value and isinstance(item.id, str)
    ]
    if not alarm_ids:
        return responses
    reads_map = read_owner_ids_by_message_ids(db, alarm_ids)
    enriched: list[ZoneMessageResponse] = []
    for item in responses:
        if item.category != MessageCategory.ALARM.value or not isinstance(item.id, str):
            enriched.append(item)
            continue
        read_ids = reads_map.get(str(item.id), [])
        enriched.append(
            item.model_copy(
                update={
                    "read_by_owner_ids": read_ids,
                    "is_read_by_viewer": viewer_id in read_ids,
                }
            )
        )
    return enriched


def record_alarm_read(
    db: Session,
    *,
    message_event_id: str,
    owner: Owner,
) -> dict:
    event = db.get(ZoneMessageEvent, message_event_id)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    _assert_alarm_event(event)
    if not _event_visible_to_owner(event, owner.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot mark this alarm as read.",
        )

    existing = (
        db.query(AlarmMessageRead)
        .filter(
            AlarmMessageRead.message_event_id == message_event_id,
            AlarmMessageRead.owner_id == owner.id,
        )
        .first()
    )
    if existing:
        existing.created_at = datetime.utcnow()
        row = existing
    else:
        row = AlarmMessageRead(
            message_event_id=message_event_id,
            owner_id=owner.id,
        )
        db.add(row)
    db.flush()

    read_ids = read_owner_ids_by_message_ids(db, [message_event_id]).get(message_event_id, [])
    return {
        "message_event_id": message_event_id,
        "owner_id": owner.id,
        "read_by_owner_ids": read_ids,
        "is_read_by_viewer": True,
        "created_at": row.created_at.isoformat(),
    }


def record_alarm_reads(
    db: Session,
    *,
    message_event_ids: list[str],
    owner: Owner,
) -> dict:
    unique_ids = [mid.strip() for mid in message_event_ids if isinstance(mid, str) and mid.strip()]
    marked: list[str] = []
    skipped: list[str] = []
    for message_event_id in unique_ids:
        event = db.get(ZoneMessageEvent, message_event_id)
        if not event or event.category != MessageCategory.ALARM:
            skipped.append(message_event_id)
            continue
        if not _event_visible_to_owner(event, owner.id):
            skipped.append(message_event_id)
            continue
        existing = (
            db.query(AlarmMessageRead)
            .filter(
                AlarmMessageRead.message_event_id == message_event_id,
                AlarmMessageRead.owner_id == owner.id,
            )
            .first()
        )
        if existing:
            existing.created_at = datetime.utcnow()
        else:
            db.add(
                AlarmMessageRead(
                    message_event_id=message_event_id,
                    owner_id=owner.id,
                )
            )
        marked.append(message_event_id)
    db.flush()
    return {
        "marked_message_ids": marked,
        "skipped_message_ids": skipped,
        "marked_count": len(marked),
    }
