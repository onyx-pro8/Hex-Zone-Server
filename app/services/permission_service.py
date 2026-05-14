"""Permission message workflow with schedule checks."""
from datetime import datetime

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import AccessSchedule, Owner, ZoneMessageEvent
from app.models.owner import OwnerRole
from app.schemas.message_feature import PropagationMessageCreate
from app.domain.message_types import CanonicalMessageType, type_category, type_scope
from app.services.guest_access_service import pick_co_owner_for_direct_permission
from app.domain.event_id import canonical_event_id, event_id_lowercase_sql_in_values


def _build_schedule_query(db: Session, zone_id: str, payload: PropagationMessageCreate):
    guest_name = str(payload.msg.get("guest_name") or "").strip()
    event_id = str(payload.msg.get("event_id") or "").strip()
    guest_id = str(payload.msg.get("guest_id") or "").strip()
    now = datetime.utcnow()

    query = db.query(AccessSchedule).filter(
        AccessSchedule.zone_id == zone_id,
        AccessSchedule.active.is_(True),
        or_(AccessSchedule.starts_at.is_(None), AccessSchedule.starts_at <= now),
        or_(AccessSchedule.ends_at.is_(None), AccessSchedule.ends_at >= now),
    )
    filters = []
    if event_id:
        canon = canonical_event_id(event_id)
        if canon:
            variants = event_id_lowercase_sql_in_values(canon)
            filters.append(func.lower(AccessSchedule.event_id).in_(variants))
    if guest_id:
        filters.append(AccessSchedule.guest_id == guest_id)
    if guest_name:
        filters.append(AccessSchedule.guest_name == guest_name)
    if filters:
        query = query.filter(or_(*filters))
    return query


def process_permission_message(db: Session, sender: Owner, payload: PropagationMessageCreate) -> dict:
    zone_id = payload.to or sender.zone_id
    schedule = _build_schedule_query(db, zone_id, payload).order_by(AccessSchedule.created_at.desc()).first()
    schedule_match = schedule is not None

    if schedule_match:
        sender_message = {
            "code": "schedule_exist_source_msg",
            "text": "Guest is in schedule. Request sent to event owner.",
        }
        member_message = {
            "code": "schedule_exist_member_msg",
            "text": "Scheduled guest arrived and requested permission.",
        }
        decision = "EXPECTED_GUEST"
    else:
        sender_message = {
            "code": "not_expected_guest_msg",
            "text": "Guest not found in schedule. Waiting for manual assist.",
        }
        member_message = {
            "code": "not_expected_members_msg",
            "text": "Unknown guest requested access. Review and chat manually.",
        }
        decision = "NOT_EXPECTED_GUEST"

    delivered_owner_ids: list[int] = []
    if schedule and schedule.created_by_owner_id:
        delivered_owner_ids.append(schedule.created_by_owner_id)
    else:
        admins = db.query(Owner.id).filter(
            Owner.zone_id == zone_id,
            Owner.role == OwnerRole.ADMINISTRATOR,
            Owner.active.is_(True),
        ).all()
        delivered_owner_ids = [row[0] for row in admins]

    if schedule and schedule.notify_member_assist:
        assist_admins = db.query(Owner.id).filter(
            Owner.zone_id == zone_id,
            Owner.role == OwnerRole.ADMINISTRATOR,
            Owner.active.is_(True),
        ).all()
        for admin_id in (row[0] for row in assist_admins):
            if admin_id not in delivered_owner_ids:
                delivered_owner_ids.append(admin_id)

    receiver_id: int | None = None
    candidates = [oid for oid in delivered_owner_ids if oid != sender.id]
    if candidates:
        receiver_id = candidates[0]
    else:
        receiver_id = pick_co_owner_for_direct_permission(db, zone_id, sender.id)

    event = ZoneMessageEvent(
        zone_id=zone_id,
        sender_id=sender.id,
        receiver_id=receiver_id,
        type=CanonicalMessageType.PERMISSION.value,
        category=type_category(CanonicalMessageType.PERMISSION),
        scope=type_scope(CanonicalMessageType.PERMISSION),
        text=sender_message["text"],
        body_json=payload.msg,
        metadata_json={
            "permission_payload": payload.msg,
            "decision": decision,
            "schedule_match": schedule_match,
            "sender_message": sender_message,
            "member_message": member_message,
            "delivered_owner_ids": delivered_owner_ids,
            "permission_visibility": "direct",
        },
    )
    db.add(event)
    db.flush()

    return {
        "decision": decision,
        "schedule_match": schedule_match,
        "sender_message": sender_message,
        "member_message": member_message,
        "delivered_owner_ids": delivered_owner_ids,
    }


def create_schedule(db: Session, owner: Owner, payload: dict) -> AccessSchedule:
    schedule = AccessSchedule(created_by_owner_id=owner.id, **payload)
    db.add(schedule)
    db.flush()
    db.refresh(schedule)
    return schedule
