"""Business logic for guest pass pre-registration lifecycle."""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.guest_pass import GuestPass, GuestPassStatus
from app.models.owner import Owner, OwnerRole
from app.services.guest_access_service import zone_exists, zone_member_owner_ids, zone_staff_owner_ids

logger = logging.getLogger(__name__)


def _owner_display_name(owner: Owner) -> str:
    parts = [owner.first_name or "", owner.last_name or ""]
    full = " ".join(p for p in parts if p).strip()
    return full or f"Owner #{owner.id}"


def _is_zone_member(owner: Owner, zone_id: str) -> bool:
    return owner.zone_id == zone_id and owner.active


def _is_zone_admin(owner: Owner, zone_id: str) -> bool:
    return _is_zone_member(owner, zone_id) and owner.role == OwnerRole.ADMINISTRATOR


def create_guest_pass(
    db: Session,
    *,
    owner: Owner,
    zone_id: str,
    event_id: str,
    guest_name: str | None,
    notes: str | None,
    expires_at: datetime,
) -> dict:
    zid = zone_id.strip()
    eid = event_id.strip()

    if not _is_zone_member(owner, zid):
        return {"error": "FORBIDDEN", "message": "You are not a member of this zone.", "http_status": 403}

    if not zone_exists(db, zid):
        return {"error": "INVALID_ZONE", "message": "Unknown or inactive zone.", "http_status": 404}

    now = datetime.utcnow()
    exp = expires_at.replace(tzinfo=None) if expires_at.tzinfo else expires_at
    if exp <= now:
        return {"error": "INVALID_EXPIRY", "message": "expires_at must be in the future.", "http_status": 422}

    existing = (
        db.query(GuestPass)
        .filter(
            GuestPass.zone_id == zid,
            func.lower(GuestPass.event_id) == eid.lower(),
        )
        .first()
    )
    if existing:
        return {"error": "DUPLICATE_EVENT_ID", "message": f"event_id '{eid}' already exists for this zone.", "http_status": 409}

    row = GuestPass(
        zone_id=zid,
        event_id=eid,
        requested_by=owner.id,
        guest_name=(guest_name or "").strip() or None,
        notes=(notes or "").strip() or None,
        status=GuestPassStatus.PENDING,
        expires_at=exp,
    )
    db.add(row)
    db.flush()

    logger.info("guest_pass_created id=%s zone_id=%s event_id=%s by=%d", row.id, zid, eid, owner.id)

    return {
        "ok": True,
        "row": row,
        "requester_name": _owner_display_name(owner),
    }


def list_guest_passes(
    db: Session,
    *,
    owner: Owner,
    zone_id: str,
    status_filter: str | None = None,
) -> dict:
    zid = zone_id.strip()

    if not _is_zone_member(owner, zid):
        return {"error": "FORBIDDEN", "message": "You are not a member of this zone.", "http_status": 403}

    q = db.query(GuestPass).filter(GuestPass.zone_id == zid)

    if status_filter and status_filter.upper() != "ALL":
        sf = status_filter.upper()
        if sf in ("PENDING", "ACCEPTED", "REJECTED", "REVOKED"):
            q = q.filter(GuestPass.status == sf)

    rows = q.order_by(GuestPass.created_at.desc()).all()

    owner_ids = {r.requested_by for r in rows}
    owners_map: dict[int, str] = {}
    if owner_ids:
        for o in db.query(Owner).filter(Owner.id.in_(owner_ids)).all():
            owners_map[o.id] = _owner_display_name(o)

    now = datetime.utcnow()
    items = []
    for r in rows:
        items.append({
            "id": r.id,
            "zone_id": r.zone_id,
            "event_id": r.event_id,
            "guest_name": r.guest_name,
            "notes": r.notes,
            "status": r.status.value if isinstance(r.status, GuestPassStatus) else r.status,
            "requested_by": r.requested_by,
            "requested_by_name": owners_map.get(r.requested_by, f"Owner #{r.requested_by}"),
            "reviewed_by": r.reviewed_by,
            "used_by_guest_id": r.used_by_guest_id,
            "expires_at": r.expires_at,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "is_expired": now > r.expires_at if r.expires_at else False,
        })

    return {"ok": True, "items": items}


def accept_guest_pass(db: Session, *, owner: Owner, pass_id: str) -> dict:
    row = db.query(GuestPass).filter(GuestPass.id == pass_id).first()
    if not row:
        return {"error": "NOT_FOUND", "message": "Guest pass not found.", "http_status": 404}

    if not _is_zone_admin(owner, row.zone_id):
        return {"error": "FORBIDDEN", "message": "Administrator role is required for this zone.", "http_status": 403}

    now = datetime.utcnow()
    if row.expires_at and now >= row.expires_at:
        return {"error": "EXPIRED", "message": "Guest pass has expired.", "http_status": 400}

    status_val = row.status.value if isinstance(row.status, GuestPassStatus) else row.status
    if status_val != GuestPassStatus.PENDING.value:
        return {"error": "INVALID_STATE", "message": f"Guest pass is already {status_val}.", "http_status": 409}

    row.status = GuestPassStatus.ACCEPTED
    row.reviewed_by = owner.id
    row.updated_at = now
    db.flush()

    logger.info("guest_pass_accepted id=%s by=%d", pass_id, owner.id)
    return {"ok": True, "row": row}


def reject_guest_pass(db: Session, *, owner: Owner, pass_id: str) -> dict:
    row = db.query(GuestPass).filter(GuestPass.id == pass_id).first()
    if not row:
        return {"error": "NOT_FOUND", "message": "Guest pass not found.", "http_status": 404}

    if not _is_zone_admin(owner, row.zone_id):
        return {"error": "FORBIDDEN", "message": "Administrator role is required for this zone.", "http_status": 403}

    now = datetime.utcnow()
    if row.expires_at and now >= row.expires_at:
        return {"error": "EXPIRED", "message": "Guest pass has expired.", "http_status": 400}

    status_val = row.status.value if isinstance(row.status, GuestPassStatus) else row.status
    if status_val != GuestPassStatus.PENDING.value:
        return {"error": "INVALID_STATE", "message": f"Guest pass is already {status_val}.", "http_status": 409}

    row.status = GuestPassStatus.REJECTED
    row.reviewed_by = owner.id
    row.updated_at = now
    db.flush()

    logger.info("guest_pass_rejected id=%s by=%d", pass_id, owner.id)
    return {"ok": True, "row": row}


def revoke_guest_pass(db: Session, *, owner: Owner, pass_id: str) -> dict:
    row = db.query(GuestPass).filter(GuestPass.id == pass_id).first()
    if not row:
        return {"error": "NOT_FOUND", "message": "Guest pass not found.", "http_status": 404}

    if not _is_zone_admin(owner, row.zone_id):
        return {"error": "FORBIDDEN", "message": "Administrator role is required for this zone.", "http_status": 403}

    status_val = row.status.value if isinstance(row.status, GuestPassStatus) else row.status
    if status_val != GuestPassStatus.ACCEPTED.value:
        return {"error": "INVALID_STATE", "message": "Only ACCEPTED guest passes can be revoked.", "http_status": 409}

    now = datetime.utcnow()
    if row.expires_at and now >= row.expires_at:
        return {"error": "EXPIRED", "message": "Guest pass has expired.", "http_status": 400}

    row.status = GuestPassStatus.REVOKED
    row.updated_at = now
    db.flush()

    logger.info("guest_pass_revoked id=%s by=%d", pass_id, owner.id)
    return {"ok": True, "row": row}


def find_accepted_guest_pass_for_event(
    db: Session,
    *,
    zone_id: str,
    event_id: str,
) -> GuestPass | None:
    """Look up a valid accepted guest pass for event_id (case-insensitive)."""
    now = datetime.utcnow()
    return (
        db.query(GuestPass)
        .filter(
            GuestPass.zone_id == zone_id,
            func.lower(GuestPass.event_id) == event_id.strip().lower(),
            GuestPass.status == GuestPassStatus.ACCEPTED,
            GuestPass.expires_at > now,
            GuestPass.used_by_guest_id.is_(None),
        )
        .first()
    )


def consume_guest_pass(db: Session, guest_pass: GuestPass, guest_id: str) -> None:
    """Mark a guest pass as consumed by setting used_by_guest_id."""
    guest_pass.used_by_guest_id = guest_id
    guest_pass.updated_at = datetime.utcnow()
    db.flush()
    logger.info("guest_pass_consumed id=%s guest_id=%s", guest_pass.id, guest_id)


def build_guest_pass_ws_payload(
    db: Session,
    *,
    guest_pass: GuestPass,
    code: str,
    zone_id: str,
) -> dict:
    """Build a PERMISSION_MESSAGE WebSocket payload for guest pass lifecycle events."""
    requester = db.query(Owner).filter(Owner.id == guest_pass.requested_by).first()
    requester_name = _owner_display_name(requester) if requester else f"Owner #{guest_pass.requested_by}"
    guest_name = guest_pass.guest_name or "a guest"
    event_id = guest_pass.event_id
    status_val = guest_pass.status.value if isinstance(guest_pass.status, GuestPassStatus) else guest_pass.status

    expires_label = ""
    if guest_pass.expires_at:
        expires_label = guest_pass.expires_at.strftime("%b %d, %Y %H:%M UTC")

    if code == "GUEST_PASS_CREATED":
        decision = "NOT_EXPECTED_GUEST"
        schedule_match = False
        sender_text = f"Your guest pass request ({event_id}) for {guest_name} has been submitted."
        member_text = f"{requester_name} requested a guest pass ({event_id}) for {guest_name}, expires {expires_label}."
    elif code == "GUEST_PASS_ACCEPTED":
        decision = "EXPECTED_GUEST"
        schedule_match = True
        sender_text = f"Your guest pass ({event_id}) for {guest_name} has been approved."
        member_text = f"Admin approved guest pass {event_id} for {guest_name}."
    elif code == "GUEST_PASS_REJECTED":
        decision = "NOT_EXPECTED_GUEST"
        schedule_match = False
        sender_text = f"Your guest pass ({event_id}) for {guest_name} has been rejected."
        member_text = f"Admin rejected guest pass {event_id}."
    else:
        decision = "NOT_EXPECTED_GUEST"
        schedule_match = False
        sender_text = f"Guest pass ({event_id}) status changed."
        member_text = f"Guest pass {event_id} status changed."

    member_ids = list(zone_staff_owner_ids(db, zone_id))

    return {
        "type": "PERMISSION_MESSAGE",
        "data": {
            "decision": decision,
            "schedule_match": schedule_match,
            "sender_message": {"code": code, "text": sender_text},
            "member_message": {"code": code, "text": member_text},
            "delivered_owner_ids": member_ids,
            "guest_pass": {
                "id": guest_pass.id,
                "event_id": event_id,
                "guest_name": guest_pass.guest_name,
                "status": status_val,
                "requested_by": guest_pass.requested_by,
                "requested_by_name": requester_name,
                "expires_at": guest_pass.expires_at.isoformat() + "Z" if guest_pass.expires_at else None,
            },
        },
    }
