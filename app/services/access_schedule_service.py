"""Access schedule create / approve / reject / revoke + admin notifications."""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.domain.message_types import CanonicalMessageType, type_category, type_scope
from app.domain.permission_visibility import PERMISSION_VISIBILITY_DIRECT
from app.models import AccessSchedule, Owner, ZoneMessageEvent
from app.models.access_schedule import AccessScheduleStatus
from app.models.owner import OwnerRole
from app.services.guest_access_service import (
    pick_co_owner_for_direct_permission,
    zone_exists,
)

logger = logging.getLogger(__name__)


def _owner_display_name(owner: Owner | None) -> str:
    if owner is None:
        return "Unknown"
    parts = [owner.first_name or "", owner.last_name or ""]
    full = " ".join(p for p in parts if p).strip()
    return full or f"Owner #{owner.id}"


def _is_zone_member(owner: Owner, zone_id: str) -> bool:
    return bool(owner.active) and (owner.zone_id or "").strip() == (zone_id or "").strip()


def _is_zone_admin(owner: Owner, zone_id: str) -> bool:
    return _is_zone_member(owner, zone_id) and owner.role == OwnerRole.ADMINISTRATOR


def zone_administrator_ids(db: Session, zone_id: str) -> list[int]:
    """Active administrator owner ids for this network zone id."""
    zid = (zone_id or "").strip()
    if not zid:
        return []
    rows = (
        db.query(Owner.id)
        .filter(
            Owner.zone_id == zid,
            Owner.role == OwnerRole.ADMINISTRATOR,
            Owner.active.is_(True),
        )
        .order_by(Owner.id.asc())
        .all()
    )
    return [row[0] for row in rows]


def _status_value(schedule: AccessSchedule) -> str:
    status = schedule.status
    return status.value if isinstance(status, AccessScheduleStatus) else str(status)


def create_schedule(db: Session, owner: Owner, payload: dict) -> dict:
    zid = str(payload.get("zone_id") or "").strip()
    if not zid:
        return {"error": "INVALID_ZONE", "message": "zone_id is required.", "http_status": 422}
    if not _is_zone_member(owner, zid):
        return {
            "error": "FORBIDDEN",
            "message": "You are not a member of this zone.",
            "http_status": 403,
        }
    if not zone_exists(db, zid):
        return {"error": "INVALID_ZONE", "message": "Unknown or inactive zone.", "http_status": 404}

    admin_created = _is_zone_admin(owner, zid)
    status = AccessScheduleStatus.ACCEPTED if admin_created else AccessScheduleStatus.PENDING
    active = admin_created

    schedule = AccessSchedule(
        zone_id=zid,
        event_id=(str(payload.get("event_id") or "").strip() or None),
        guest_id=(str(payload.get("guest_id") or "").strip() or None),
        guest_name=(str(payload.get("guest_name") or "").strip() or None),
        starts_at=payload.get("starts_at"),
        ends_at=payload.get("ends_at"),
        notify_member_assist=bool(payload.get("notify_member_assist") or False),
        active=active,
        status=status,
        created_by_owner_id=owner.id,
        reviewed_by=owner.id if admin_created else None,
    )
    db.add(schedule)
    db.flush()
    db.refresh(schedule)

    code = "SCHEDULE_ACCEPTED" if admin_created else "SCHEDULE_CREATED"
    _record_schedule_permission_zone_event(db, schedule=schedule, code=code, acting_owner_id=owner.id)
    ws_payload = build_schedule_ws_payload(db, schedule=schedule, code=code, acting_owner_id=owner.id)

    logger.info(
        "access_schedule_created id=%s zone_id=%s status=%s by=%d",
        schedule.id,
        zid,
        _status_value(schedule),
        owner.id,
    )

    return {
        "ok": True,
        "row": schedule,
        "ws_payload": ws_payload,
        "delivered_owner_ids": list(ws_payload["data"]["delivered_owner_ids"]),
    }


def list_schedules(
    db: Session,
    *,
    owner: Owner,
    zone_id: str,
    status_filter: str | None = None,
) -> dict:
    zid = zone_id.strip()
    if not _is_zone_member(owner, zid):
        return {
            "error": "FORBIDDEN",
            "message": "You are not a member of this zone.",
            "http_status": 403,
        }

    q = db.query(AccessSchedule).filter(AccessSchedule.zone_id == zid)
    if status_filter and status_filter.upper() != "ALL":
        sf = status_filter.upper()
        if sf in ("PENDING", "ACCEPTED", "REJECTED", "REVOKED"):
            q = q.filter(AccessSchedule.status == sf)

    rows = q.order_by(AccessSchedule.created_at.desc()).all()
    return {"ok": True, "items": rows}


def accept_schedule(db: Session, *, owner: Owner, schedule_id: int) -> dict:
    row = db.query(AccessSchedule).filter(AccessSchedule.id == schedule_id).first()
    if not row:
        return {"error": "NOT_FOUND", "message": "Schedule not found.", "http_status": 404}
    if not _is_zone_admin(owner, row.zone_id):
        return {
            "error": "FORBIDDEN",
            "message": "Administrator role is required for this zone.",
            "http_status": 403,
        }
    if row.status != AccessScheduleStatus.PENDING and _status_value(row) != "PENDING":
        return {
            "error": "INVALID_STATUS",
            "message": f"Only PENDING schedules can be accepted (current: {_status_value(row)}).",
            "http_status": 409,
        }

    row.status = AccessScheduleStatus.ACCEPTED
    row.active = True
    row.reviewed_by = owner.id
    row.updated_at = datetime.utcnow()
    db.flush()

    _record_schedule_permission_zone_event(
        db, schedule=row, code="SCHEDULE_ACCEPTED", acting_owner_id=owner.id
    )
    ws_payload = build_schedule_ws_payload(
        db, schedule=row, code="SCHEDULE_ACCEPTED", acting_owner_id=owner.id
    )
    return {
        "ok": True,
        "row": row,
        "ws_payload": ws_payload,
        "delivered_owner_ids": list(ws_payload["data"]["delivered_owner_ids"]),
    }


def reject_schedule(db: Session, *, owner: Owner, schedule_id: int) -> dict:
    row = db.query(AccessSchedule).filter(AccessSchedule.id == schedule_id).first()
    if not row:
        return {"error": "NOT_FOUND", "message": "Schedule not found.", "http_status": 404}
    if not _is_zone_admin(owner, row.zone_id):
        return {
            "error": "FORBIDDEN",
            "message": "Administrator role is required for this zone.",
            "http_status": 403,
        }
    if row.status != AccessScheduleStatus.PENDING and _status_value(row) != "PENDING":
        return {
            "error": "INVALID_STATUS",
            "message": f"Only PENDING schedules can be rejected (current: {_status_value(row)}).",
            "http_status": 409,
        }

    row.status = AccessScheduleStatus.REJECTED
    row.active = False
    row.reviewed_by = owner.id
    row.updated_at = datetime.utcnow()
    db.flush()

    _record_schedule_permission_zone_event(
        db, schedule=row, code="SCHEDULE_REJECTED", acting_owner_id=owner.id
    )
    ws_payload = build_schedule_ws_payload(
        db, schedule=row, code="SCHEDULE_REJECTED", acting_owner_id=owner.id
    )
    return {
        "ok": True,
        "row": row,
        "ws_payload": ws_payload,
        "delivered_owner_ids": list(ws_payload["data"]["delivered_owner_ids"]),
    }


def revoke_schedule(db: Session, *, owner: Owner, schedule_id: int) -> dict:
    row = db.query(AccessSchedule).filter(AccessSchedule.id == schedule_id).first()
    if not row:
        return {"error": "NOT_FOUND", "message": "Schedule not found.", "http_status": 404}
    if not _is_zone_admin(owner, row.zone_id):
        return {
            "error": "FORBIDDEN",
            "message": "Administrator role is required for this zone.",
            "http_status": 403,
        }
    if row.status != AccessScheduleStatus.ACCEPTED and _status_value(row) != "ACCEPTED":
        return {
            "error": "INVALID_STATUS",
            "message": f"Only ACCEPTED schedules can be revoked (current: {_status_value(row)}).",
            "http_status": 409,
        }

    row.status = AccessScheduleStatus.REVOKED
    row.active = False
    row.reviewed_by = owner.id
    row.updated_at = datetime.utcnow()
    db.flush()

    _record_schedule_permission_zone_event(
        db, schedule=row, code="SCHEDULE_REVOKED", acting_owner_id=owner.id
    )
    ws_payload = build_schedule_ws_payload(
        db, schedule=row, code="SCHEDULE_REVOKED", acting_owner_id=owner.id
    )
    return {
        "ok": True,
        "row": row,
        "ws_payload": ws_payload,
        "delivered_owner_ids": list(ws_payload["data"]["delivered_owner_ids"]),
    }


def schedule_permission_direct_recipient_ids(
    db: Session,
    *,
    schedule: AccessSchedule,
    code: str,
    acting_owner_id: int | None = None,
) -> list[int]:
    """Owner ids that should receive WS + see the lifecycle in the merged inbox."""
    zid = schedule.zone_id
    creator_id = schedule.created_by_owner_id
    out: set[int] = set()

    if code == "SCHEDULE_CREATED":
        # All network admins must see the pending request; include the requester.
        out.update(zone_administrator_ids(db, zid))
        if creator_id is not None:
            out.add(creator_id)
        if not out and creator_id is not None:
            out.add(pick_co_owner_for_direct_permission(db, zid, creator_id))
            out.add(creator_id)
    elif code in ("SCHEDULE_ACCEPTED", "SCHEDULE_REJECTED", "SCHEDULE_REVOKED"):
        if acting_owner_id is not None:
            out.add(acting_owner_id)
        if creator_id is not None:
            out.add(creator_id)
        out.update(zone_administrator_ids(db, zid))
    else:
        if creator_id is not None:
            out.add(creator_id)
        out.update(zone_administrator_ids(db, zid))

    return sorted(x for x in out if x is not None)


def build_schedule_ws_payload(
    db: Session,
    *,
    schedule: AccessSchedule,
    code: str,
    acting_owner_id: int | None = None,
) -> dict:
    creator = (
        db.query(Owner).filter(Owner.id == schedule.created_by_owner_id).first()
        if schedule.created_by_owner_id
        else None
    )
    creator_name = _owner_display_name(creator) if creator else "A member"
    guest_name_raw = (schedule.guest_name or "").strip()
    event_id = (schedule.event_id or "").strip()
    status_val = _status_value(schedule)
    for_guest = f" for {guest_name_raw}" if guest_name_raw else ""
    event_bit = f" (Event ID: {event_id})" if event_id else ""

    window_bits: list[str] = []
    if schedule.starts_at:
        window_bits.append(schedule.starts_at.strftime("%b %d, %Y %H:%M UTC"))
    if schedule.ends_at:
        window_bits.append(schedule.ends_at.strftime("%b %d, %Y %H:%M UTC"))
    window_label = " → ".join(window_bits) if window_bits else "open window"

    if code == "SCHEDULE_CREATED":
        decision = "NOT_EXPECTED_GUEST"
        schedule_match = False
        sender_text = "Your guest schedule request was submitted and is pending admin review."
        member_text = (
            f"{creator_name} requested a guest schedule{event_bit}{for_guest}, "
            f"{window_label}. Approve or reject in Guest schedules."
        )
    elif code == "SCHEDULE_ACCEPTED":
        decision = "EXPECTED_GUEST"
        schedule_match = True
        sender_text = "Your guest schedule was approved."
        member_text = (
            f"Admin approved guest schedule{event_bit}{for_guest}. "
            f"Matching guests will be auto-approved during {window_label}."
        )
    elif code == "SCHEDULE_REJECTED":
        decision = "NOT_EXPECTED_GUEST"
        schedule_match = False
        sender_text = "Your guest schedule was rejected."
        member_text = f"Admin rejected guest schedule{event_bit}{for_guest}."
    elif code == "SCHEDULE_REVOKED":
        decision = "NOT_EXPECTED_GUEST"
        schedule_match = False
        sender_text = "Your guest schedule was revoked."
        member_text = (
            f"Admin revoked guest schedule{event_bit}{for_guest}. "
            f"It will no longer auto-approve guests."
        )
    else:
        decision = "NOT_EXPECTED_GUEST"
        schedule_match = False
        sender_text = "Guest schedule status changed."
        member_text = f"Guest schedule{event_bit} status changed."

    member_ids = schedule_permission_direct_recipient_ids(
        db, schedule=schedule, code=code, acting_owner_id=acting_owner_id
    )

    return {
        "type": "PERMISSION_MESSAGE",
        "data": {
            "decision": decision,
            "schedule_match": schedule_match,
            "sender_message": {"code": code, "text": sender_text},
            "member_message": {"code": code, "text": member_text},
            "delivered_owner_ids": member_ids,
            "access_schedule": {
                "id": schedule.id,
                "event_id": schedule.event_id,
                "guest_name": schedule.guest_name,
                "guest_id": schedule.guest_id,
                "status": status_val,
                "active": bool(schedule.active),
                "created_by_owner_id": schedule.created_by_owner_id,
                "created_by_name": creator_name,
                "starts_at": schedule.starts_at.isoformat() + "Z" if schedule.starts_at else None,
                "ends_at": schedule.ends_at.isoformat() + "Z" if schedule.ends_at else None,
            },
        },
    }


def _record_schedule_permission_zone_event(
    db: Session,
    *,
    schedule: AccessSchedule,
    code: str,
    acting_owner_id: int | None = None,
) -> None:
    payload = build_schedule_ws_payload(
        db, schedule=schedule, code=code, acting_owner_id=acting_owner_id
    )
    member_text = payload["data"]["member_message"]["text"]
    status_val = _status_value(schedule)

    if code == "SCHEDULE_CREATED":
        sender_id = schedule.created_by_owner_id
    else:
        sender_id = acting_owner_id or schedule.reviewed_by or schedule.created_by_owner_id

    direct_ids = schedule_permission_direct_recipient_ids(
        db, schedule=schedule, code=code, acting_owner_id=acting_owner_id
    )
    receiver_id = next(
        (oid for oid in direct_ids if sender_id is None or oid != sender_id),
        sender_id,
    )

    body: dict = {
        "access_schedule_id": schedule.id,
        "event_id": schedule.event_id,
        "code": code,
        "status": status_val,
        "created_by_owner_id": schedule.created_by_owner_id,
        "guest_name": schedule.guest_name,
        "guest_id": schedule.guest_id,
        "zone_id": schedule.zone_id,
        "starts_at": schedule.starts_at.isoformat() + "Z" if schedule.starts_at else None,
        "ends_at": schedule.ends_at.isoformat() + "Z" if schedule.ends_at else None,
    }
    if schedule.reviewed_by is not None:
        body["reviewed_by"] = schedule.reviewed_by

    perm = ZoneMessageEvent(
        zone_id=schedule.zone_id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        guest_access_session_id=None,
        type=CanonicalMessageType.PERMISSION.value,
        category=type_category(CanonicalMessageType.PERMISSION),
        scope=type_scope(CanonicalMessageType.PERMISSION),
        text=member_text,
        body_json=body,
        metadata_json={
            "flow": "guest_schedule_lifecycle",
            "domain_event": code,
            "permission_visibility": PERMISSION_VISIBILITY_DIRECT,
            "delivered_owner_ids": direct_ids,
        },
    )
    db.add(perm)
    db.flush()
