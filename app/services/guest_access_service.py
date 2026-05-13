"""QR guest arrival: zone validation, schedule match, sessions, notifications."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.config import settings

from app.domain.message_types import CanonicalMessageType, type_category, type_scope
from app.models import AccessSchedule, GuestAccessSession, Owner, Zone, ZoneMessageEvent
from app.models.owner import OwnerRole
from app.services.access_policy import zone_listing_owner_ids

logger = logging.getLogger(__name__)


def mint_guest_exchange_for_session(row: GuestAccessSession) -> None:
    """Issue a fresh one-time JWT exchange secret for this session row (not flushed)."""
    ttl = max(1, int(settings.GUEST_ACCESS_EXCHANGE_TTL_MINUTES))
    row.exchange_code = str(uuid.uuid4())
    row.exchange_expires_at = datetime.utcnow() + timedelta(minutes=ttl)
    row.exchange_consumed_at = None


def guest_exchange_expires_at_iso(row: GuestAccessSession) -> str:
    exp = row.exchange_expires_at
    assert exp is not None
    return exp.replace(microsecond=0).isoformat() + "Z"


def ensure_active_guest_exchange_for_poll(db: Session, row: GuestAccessSession) -> bool:
    """If the guest is cleared for the guest app but has no usable code, mint one. Returns True if DB was mutated."""
    cleared = row.kind == "expected" or (row.kind == "unexpected" and row.resolution == "approved")
    if not cleared or row.exchange_consumed_at is not None:
        return False
    now = datetime.utcnow()
    if (
        row.exchange_code
        and row.exchange_expires_at
        and row.exchange_expires_at > now
    ):
        return False
    mint_guest_exchange_for_session(row)
    db.flush()
    return True


def zone_exists(db: Session, zone_id: str) -> bool:
    z = db.query(Zone.id).filter(Zone.zone_id == zone_id, Zone.active.is_(True)).first()
    if z:
        return True
    return (
        db.query(Owner.id)
        .filter(Owner.zone_id == zone_id, Owner.active.is_(True))
        .first()
        is not None
    )


def can_manage_zone_guest_requests(db: Session, viewer: Owner, zone_id: str) -> bool:
    """True if the owner may list guest arrivals and send PERMISSION/CHAT to guests for this zone id."""
    zid = (zone_id or "").strip()
    if not zid:
        return False
    allowed_ids = zone_listing_owner_ids(db, viewer)
    if not allowed_ids:
        return False
    if (
        db.query(Zone.id)
        .filter(Zone.zone_id == zid, Zone.active.is_(True), Zone.owner_id.in_(allowed_ids))
        .first()
    ):
        return True
    if viewer.zone_id == zid and zone_exists(db, zid):
        return True
    if viewer.role == OwnerRole.ADMINISTRATOR:
        return (
            db.query(Owner.id)
            .filter(Owner.zone_id == zid, Owner.active.is_(True), Owner.id.in_(allowed_ids))
            .first()
            is not None
        )
    return False


def _guest_row_client_status(row: GuestAccessSession) -> str:
    """PENDING / APPROVED / REJECTED for dashboard approval UI."""
    if row.kind == "expected":
        return "APPROVED"
    if row.resolution == "approved":
        return "APPROVED"
    if row.resolution == "rejected":
        return "REJECTED"
    return "PENDING"


def resolve_primary_zone_admin_owner(db: Session, zone_id: str) -> Owner | None:
    """Prefer the main **zones** row's **owner_id** without loading PostGIS geometry (SQLite-safe)."""
    zid = zone_id.strip()
    zone_owner_id = (
        db.query(Zone.owner_id)
        .filter(Zone.zone_id == zid, Zone.active.is_(True))
        .order_by(Zone.id.asc())
        .limit(1)
        .scalar()
    )
    if zone_owner_id is not None:
        owner = db.get(Owner, zone_owner_id)
        if owner and owner.active:
            return owner
    return (
        db.query(Owner)
        .filter(
            Owner.zone_id == zone_id,
            Owner.role == OwnerRole.ADMINISTRATOR,
            Owner.active.is_(True),
        )
        .order_by(Owner.id.asc())
        .first()
    )


def find_matching_schedule_for_arrival(
    db: Session,
    zone_id: str,
    *,
    guest_name: str,
    event_id: str | None,
) -> AccessSchedule | None:
    """Schedule match: zone + time window + (event_id match OR guest_name match)."""
    gn = guest_name.strip()
    ev = (event_id or "").strip()
    conditions = []
    if gn:
        conditions.append(AccessSchedule.guest_name == gn)
    if ev:
        conditions.append(AccessSchedule.event_id == ev)
    if not conditions:
        return None

    now = datetime.utcnow()
    q = db.query(AccessSchedule).filter(
        AccessSchedule.zone_id == zone_id,
        AccessSchedule.active.is_(True),
        or_(AccessSchedule.starts_at.is_(None), AccessSchedule.starts_at <= now),
        or_(AccessSchedule.ends_at.is_(None), AccessSchedule.ends_at >= now),
        or_(*conditions),
    )
    return q.order_by(AccessSchedule.created_at.desc()).first()


def zone_staff_owner_ids(db: Session, zone_id: str) -> set[int]:
    """Active **owners.id** values considered zone hosts/staff for a shared **zone_id** string.

    Combines: (1) owners whose **`owners.zone_id`** matches (signup / primary zone), (2) owners who **own**
    an active **`zones`** row for this **`zone_id`**, and (3) **`resolve_primary_zone_admin_owner`** so at least
    one administrator appears when data is split across **`owners`** vs **`zones`** tables.
    Used for guest **peers**, unexpected-guest WebSocket targets (`zone_member_owner_ids`), and must stay aligned.
    """

    zid = (zone_id or "").strip()
    if not zid:
        return set()
    ids: set[int] = set()
    for (oid,) in db.query(Owner.id).filter(Owner.zone_id == zid, Owner.active.is_(True)).all():
        ids.add(oid)
    for (oid,) in (
        db.query(Zone.owner_id)
        .filter(Zone.zone_id == zid, Zone.active.is_(True))
        .distinct()
        .all()
    ):
        ids.add(oid)
    primary = resolve_primary_zone_admin_owner(db, zid)
    if primary and primary.active:
        ids.add(primary.id)
    return ids


def zone_member_owner_ids(db: Session, zone_id: str) -> list[int]:
    """Sorted list of active owners to notify for unexpected guest (same cohort as **GET …/peers**)."""

    return sorted(zone_staff_owner_ids(db, zone_id))


def process_guest_arrival(
    db: Session,
    *,
    zone_id: str,
    guest_name: str,
    event_id: str | None,
    device_id: str | None,
    latitude: float | None,
    longitude: float | None,
    qr_token_db_id: int | None = None,
) -> dict:
    """Persist guest session, permission event, return HTTP-facing payload + websocket targets."""
    if not zone_exists(db, zone_id):
        return {"error": "INVALID_ZONE", "message": "Unknown or inactive zone.", "http_status": 404}

    # Guest pass lookup: if event_id provided, check for a valid ACCEPTED guest pass first.
    ev_id = (event_id or "").strip()
    if ev_id:
        from app.services import guest_pass_service as _gp_service  # deferred to avoid circular import

        guest_pass = _gp_service.find_accepted_guest_pass_for_event(db, zone_id=zone_id, event_id=ev_id)
        if guest_pass:
            guest_token = str(uuid.uuid4())
            _gp_service.consume_guest_pass(db, guest_pass, guest_token)

            session_row = GuestAccessSession(
                guest_id=guest_token,
                zone_id=zone_id,
                guest_name=guest_name.strip(),
                event_id=ev_id,
                device_id=(device_id or "").strip() or None,
                latitude=latitude,
                longitude=longitude,
                kind="expected",
                resolution=None,
                schedule_id=None,
                admin_owner_id=None,
                qr_token_id=qr_token_db_id,
            )
            db.add(session_row)
            db.flush()
            mint_guest_exchange_for_session(session_row)
            db.flush()

            broadcast_ids = zone_member_owner_ids(db, zone_id)
            ws_guest_is_here: list[tuple[list[int], dict]] = []
            payload_here = {
                "type": "guest_is_here",
                "guest_name": guest_name.strip(),
                "zone_id": zone_id,
                "guest_id": guest_token,
                "event_id": ev_id,
                "guest_pass_id": guest_pass.id,
            }
            if broadcast_ids:
                ws_guest_is_here.append((broadcast_ids, payload_here))

            permission_sender_owner_id = guest_pass.requested_by
            perm_event = ZoneMessageEvent(
                zone_id=zone_id,
                sender_id=permission_sender_owner_id,
                guest_access_session_id=session_row.id,
                type=CanonicalMessageType.PERMISSION.value,
                category=type_category(CanonicalMessageType.PERMISSION),
                scope=type_scope(CanonicalMessageType.PERMISSION),
                text=f"Guest pass verified for {guest_name.strip()} (event {ev_id}).",
                body_json={
                    "guest_name": guest_name.strip(),
                    "zone_id": zone_id,
                    "guest_id": guest_token,
                    "guest_request_id": session_row.id,
                    "event_id": ev_id,
                    "device_id": (device_id or "").strip() or None,
                    "location": {"lat": latitude, "lng": longitude},
                    "guest_message": "You are expected. Guest pass verified.",
                    "guest_pass_id": guest_pass.id,
                },
                metadata_json={
                    "flow": "guest_pass_arrival",
                    "guest_id": guest_token,
                    "guest_access_session_db_id": session_row.id,
                    "guest_pass_id": guest_pass.id,
                    "schedule_match": True,
                    "domain_event": "guest_pass_arrival",
                    **({"guest_access_qr_token_db_id": qr_token_db_id} if qr_token_db_id is not None else {}),
                },
            )
            db.add(perm_event)
            db.flush()

            return {
                "guest_response": {
                    "status": "EXPECTED",
                    "message": "You are expected. Guest pass verified.",
                    "guest_id": guest_token,
                    "zone_id": zone_id,
                    "exchange_code": session_row.exchange_code,
                    "exchange_expires_at": guest_exchange_expires_at_iso(session_row),
                },
                "ws_guest_is_here": ws_guest_is_here,
                "ws_unexpected_guest": [],
            }

    schedule = find_matching_schedule_for_arrival(db, zone_id, guest_name=guest_name, event_id=event_id)
    guest_token = str(uuid.uuid4())

    ws_guest_is_here: list[tuple[list[int], dict]] = []
    ws_unexpected: list[tuple[list[int], dict]] = []

    permission_sender_owner_id: int | None = None

    if schedule:
        session_row = GuestAccessSession(
            guest_id=guest_token,
            zone_id=zone_id,
            guest_name=guest_name.strip(),
            event_id=(event_id or "").strip() or None,
            device_id=(device_id or "").strip() or None,
            latitude=latitude,
            longitude=longitude,
            kind="expected",
            resolution=None,
            schedule_id=schedule.id,
            admin_owner_id=None,
            qr_token_id=qr_token_db_id,
        )
        db.add(session_row)
        db.flush()
        mint_guest_exchange_for_session(session_row)
        db.flush()

        notify_ids: list[int] = []
        if schedule.created_by_owner_id:
            notify_ids.append(schedule.created_by_owner_id)
        else:
            notify_ids = [
                row[0]
                for row in db.query(Owner.id)
                .filter(
                    Owner.zone_id == zone_id,
                    Owner.role == OwnerRole.ADMINISTRATOR,
                    Owner.active.is_(True),
                )
                .all()
            ]

        if schedule.notify_member_assist:
            admin_rows = (
                db.query(Owner.id)
                .filter(
                    Owner.zone_id == zone_id,
                    Owner.role == OwnerRole.ADMINISTRATOR,
                    Owner.active.is_(True),
                )
                .all()
            )
            for aid in (row[0] for row in admin_rows):
                if aid not in notify_ids:
                    notify_ids.append(aid)

        payload_here = {
            "type": "guest_is_here",
            "guest_name": guest_name.strip(),
            "zone_id": zone_id,
            "guest_id": guest_token,
            "event_id": (event_id or "").strip() or None,
        }
        if notify_ids:
            ws_guest_is_here.append((notify_ids, payload_here))
        if notify_ids:
            permission_sender_owner_id = notify_ids[0]

        perm_meta = {
            "flow": "qr_guest_arrival",
            "guest_id": guest_token,
            "guest_access_session_db_id": session_row.id,
            "schedule_match": True,
            "websocket_events": [{"name": "guest_is_here", "targets": "schedule_owner_and_optional_assist"}],
            "domain_event": "guest_expected_arrival",
            **({"guest_access_qr_token_db_id": qr_token_db_id} if qr_token_db_id is not None else {}),
        }
        decision = "EXPECTED"
        msg_guest = "You are expected. Please proceed."
        perm_line = f"Expected guest arrived: {guest_name.strip()}."
    else:
        admin = resolve_primary_zone_admin_owner(db, zone_id)
        if not admin:
            return {"error": "NO_ZONE_ADMIN", "message": "No administrator found for this zone.", "http_status": 422}
        permission_sender_owner_id = admin.id

        session_row = GuestAccessSession(
            guest_id=guest_token,
            zone_id=zone_id,
            guest_name=guest_name.strip(),
            event_id=(event_id or "").strip() or None,
            device_id=(device_id or "").strip() or None,
            latitude=latitude,
            longitude=longitude,
            kind="unexpected",
            resolution="pending",
            schedule_id=None,
            admin_owner_id=admin.id,
            qr_token_id=qr_token_db_id,
        )
        db.add(session_row)
        db.flush()

        broadcast_ids = zone_member_owner_ids(db, zone_id)
        ws_unexpected.append(
            (
                broadcast_ids,
                {
                    "type": "unexpected_guest",
                    "guest_name": guest_name.strip(),
                    "zone_id": zone_id,
                    "guest_id": guest_token,
                },
            )
        )

        perm_meta = {
            "flow": "qr_guest_arrival",
            "guest_id": guest_token,
            "guest_access_session_db_id": session_row.id,
            "schedule_match": False,
            "websocket_events": [{"name": "unexpected_guest", "targets": "zone_members"}],
            "domain_event": "guest_request_created",
            **({"guest_access_qr_token_db_id": qr_token_db_id} if qr_token_db_id is not None else {}),
        }
        decision = "UNEXPECTED"
        msg_guest = "You are not scheduled. Please wait for approval."
        perm_line = f"Guest access requested for {guest_name.strip()}. Awaiting approval."

    perm_event = ZoneMessageEvent(
        zone_id=zone_id,
        sender_id=permission_sender_owner_id,
        guest_access_session_id=session_row.id,
        type=CanonicalMessageType.PERMISSION.value,
        category=type_category(CanonicalMessageType.PERMISSION),
        scope=type_scope(CanonicalMessageType.PERMISSION),
        text=perm_line,
        body_json={
            "guest_name": guest_name.strip(),
            "zone_id": zone_id,
            "guest_id": guest_token,
            "guest_request_id": session_row.id,
            "event_id": (event_id or "").strip() or None,
            "device_id": (device_id or "").strip() or None,
            "location": {"lat": latitude, "lng": longitude},
            "guest_message": msg_guest,
        },
        metadata_json=perm_meta,
    )
    db.add(perm_event)
    db.flush()

    guest_response: dict = {
        "status": decision,
        "message": msg_guest,
        "guest_id": guest_token,
        "zone_id": zone_id,
    }
    if decision == "EXPECTED":
        guest_response["exchange_code"] = session_row.exchange_code
        guest_response["exchange_expires_at"] = guest_exchange_expires_at_iso(session_row)

    return {
        "guest_response": guest_response,
        "ws_guest_is_here": ws_guest_is_here,
        "ws_unexpected_guest": ws_unexpected,
    }


def serialize_guest_session_row(row: GuestAccessSession) -> dict:
    """Member/API list shape for dashboard polling."""
    base = guest_session_public_view(row)
    return {
        "id": row.id,
        "guest_id": row.guest_id,
        "zone_id": row.zone_id,
        "guest_name": row.guest_name,
        "event_id": row.event_id,
        "device_id": row.device_id,
        "hid": row.device_id,
        "kind": row.kind,
        "resolution": row.resolution,
        "schedule_id": row.schedule_id,
        "admin_owner_id": row.admin_owner_id,
        "qr_token_id": row.qr_token_id,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "created_at": row.created_at,
        "guest_status": base["status"],
        "status": _guest_row_client_status(row),
        "expectation": row.kind,
    }


def get_guest_access_session_by_guest_id(db: Session, guest_id: str) -> GuestAccessSession | None:
    """Return the guest row for **guest_id** (opaque id is globally unique per session)."""
    gid = guest_id.strip()
    if not gid:
        return None
    return db.query(GuestAccessSession).filter(GuestAccessSession.guest_id == gid).first()


def list_guest_sessions_for_zone(
    db: Session,
    *,
    zone_id: str,
    limit: int = 50,
    skip: int = 0,
    pending_only: bool = False,
    status: str | None = None,
) -> list[GuestAccessSession]:
    lim = max(1, min(int(limit), 200))
    sk = max(0, min(int(skip), 10_000))
    q = db.query(GuestAccessSession).filter(GuestAccessSession.zone_id == zone_id.strip())
    if pending_only:
        q = q.filter(GuestAccessSession.kind == "unexpected", GuestAccessSession.resolution == "pending")
    elif status and (st := status.strip().upper()):
        if st == "PENDING":
            q = q.filter(
                GuestAccessSession.kind == "unexpected",
                GuestAccessSession.resolution == "pending",
            )
        elif st in ("APPROVED", "GRANTED"):
            q = q.filter(
                or_(
                    GuestAccessSession.kind == "expected",
                    and_(GuestAccessSession.kind == "unexpected", GuestAccessSession.resolution == "approved"),
                )
            )
        elif st in ("REJECTED", "DENIED"):
            q = q.filter(GuestAccessSession.kind == "unexpected", GuestAccessSession.resolution == "rejected")
    return q.order_by(GuestAccessSession.created_at.desc()).offset(sk).limit(lim).all()


def guest_session_public_view(row: GuestAccessSession) -> dict:
    if row.kind == "expected":
        status = "EXPECTED"
        message = "You are expected. Please proceed."
        approval_status = "APPROVED"
    elif row.resolution == "approved":
        status = "APPROVED"
        message = "Your visit has been approved. Welcome."
        approval_status = "APPROVED"
    elif row.resolution == "rejected":
        status = "REJECTED"
        message = "Access was not approved."
        approval_status = "REJECTED"
    else:
        status = "UNEXPECTED"
        message = "You are not scheduled. Please wait for approval."
        approval_status = "PENDING"
    out = {
        "guest_id": row.guest_id,
        "zone_id": row.zone_id,
        "status": status,
        "approval_status": approval_status,
        "message": message,
    }
    ready_for_exchange = (row.kind == "expected") or (
        row.kind == "unexpected" and row.resolution == "approved"
    )
    if ready_for_exchange and row.exchange_consumed_at is None:
        now = datetime.utcnow()
        if row.exchange_code and row.exchange_expires_at and row.exchange_expires_at > now:
            out["exchange_code"] = row.exchange_code
            out["exchange_expires_at"] = row.exchange_expires_at.replace(microsecond=0).isoformat() + "Z"
    return out


def consume_guest_exchange_and_issue_context(
    db: Session,
    *,
    guest_id: str,
    zone_id: str,
    exchange_code: str,
    device_id: str | None,
) -> dict:
    """Validate one-time exchange; on success set exchange_consumed_at and return session row dict.

    Returns {"ok": True, "row": GuestAccessSession} or {"error": code, "message": str, "http_status": int}.
    """
    gid = guest_id.strip()
    zid = zone_id.strip()
    code = (exchange_code or "").strip()
    if not gid or not zid or not code:
        return {
            "error": "exchange_invalid",
            "message": "guest_id, zone_id, and exchange_code are required.",
            "http_status": 400,
        }

    row = (
        db.query(GuestAccessSession)
        .filter(GuestAccessSession.guest_id == gid)
        .with_for_update()
        .first()
    )
    if not row:
        return {"error": "NOT_FOUND", "message": "Unknown guest session.", "http_status": 404}

    if row.zone_id != zid:
        return {"error": "zone_mismatch", "message": "zone_id does not match this guest session.", "http_status": 403}

    cleared = row.kind == "expected" or (row.kind == "unexpected" and row.resolution == "approved")
    if not cleared:
        return {"error": "guest_not_approved", "message": "Guest access is not approved.", "http_status": 403}

    if row.exchange_consumed_at is not None:
        return {"error": "exchange_consumed", "message": "This exchange code was already used.", "http_status": 409}

    if not row.exchange_code or row.exchange_code != code:
        return {"error": "exchange_invalid", "message": "Invalid exchange code.", "http_status": 400}

    now = datetime.utcnow()
    if not row.exchange_expires_at or row.exchange_expires_at <= now:
        return {"error": "exchange_expired", "message": "Exchange code has expired.", "http_status": 400}

    persisted_dev = (row.device_id or "").strip() or None
    req_dev = (device_id or "").strip() or None
    if persisted_dev and req_dev and persisted_dev != req_dev:
        return {"error": "device_mismatch", "message": "device_id does not match the arrival session.", "http_status": 403}

    row.exchange_consumed_at = now
    db.flush()
    logger.info(
        "guest_exchange_consumed guest_id=%s zone_id=%s session_db_id=%s",
        gid,
        zid,
        row.id,
    )
    return {"ok": True, "row": row}


def approve_guest(db: Session, *, acting_owner: Owner, zone_id: str, guest_id: str) -> dict:
    zid = zone_id.strip()
    if acting_owner.role != OwnerRole.ADMINISTRATOR:
        return {"error": "FORBIDDEN", "message": "Administrator action required for this zone.", "http_status": 403}
    if not can_manage_zone_guest_requests(db, acting_owner, zid):
        return {
            "error": "FORBIDDEN",
            "message": "You are not allowed to approve guest requests for this zone.",
            "http_status": 403,
        }

    row = (
        db.query(GuestAccessSession)
        .filter(
            GuestAccessSession.guest_id == guest_id,
            GuestAccessSession.zone_id == zid,
        )
        .first()
    )
    if not row:
        return {"error": "NOT_FOUND", "message": "Guest session not found.", "http_status": 404}
    if row.kind != "unexpected":
        return {"error": "INVALID_STATE", "message": "Guest does not require approval.", "http_status": 422}
    if row.resolution != "pending":
        return {"error": "INVALID_STATE", "message": "Guest session already resolved.", "http_status": 422}

    row.resolution = "approved"
    mint_guest_exchange_for_session(row)
    db.flush()

    gname = (row.guest_name or "").strip() or "Guest"
    note = ZoneMessageEvent(
        zone_id=zid,
        sender_id=acting_owner.id,
        guest_access_session_id=row.id,
        type=CanonicalMessageType.PERMISSION.value,
        category=type_category(CanonicalMessageType.PERMISSION),
        scope=type_scope(CanonicalMessageType.PERMISSION),
        text=f"Guest access approved for {gname}.",
        body_json={
            "guest_id": guest_id,
            "guest_name": gname,
            "guest_request_id": row.id,
            "zone_id": zid,
            "resolution": "APPROVED",
        },
        metadata_json={
            "flow": "guest_access_approve",
            "domain_event": "guest_request_approved",
            "acting_owner_id": acting_owner.id,
            "guest_access_session_db_id": row.id,
        },
    )
    db.add(note)
    db.flush()

    return {
        "ok": True,
        "guest_response": {
            "status": "APPROVED",
            "message": "Your visit has been approved. Welcome.",
            "guest_id": guest_id,
        },
    }


def reject_guest(db: Session, *, acting_owner: Owner, zone_id: str, guest_id: str) -> dict:
    zid = zone_id.strip()
    if acting_owner.role != OwnerRole.ADMINISTRATOR:
        return {"error": "FORBIDDEN", "message": "Administrator action required for this zone.", "http_status": 403}
    if not can_manage_zone_guest_requests(db, acting_owner, zid):
        return {
            "error": "FORBIDDEN",
            "message": "You are not allowed to reject guest requests for this zone.",
            "http_status": 403,
        }

    row = (
        db.query(GuestAccessSession)
        .filter(
            GuestAccessSession.guest_id == guest_id,
            GuestAccessSession.zone_id == zid,
        )
        .first()
    )
    if not row:
        return {"error": "NOT_FOUND", "message": "Guest session not found.", "http_status": 404}
    if row.kind != "unexpected":
        return {"error": "INVALID_STATE", "message": "Guest does not require approval.", "http_status": 422}
    if row.resolution != "pending":
        return {"error": "INVALID_STATE", "message": "Guest session already resolved.", "http_status": 422}

    row.resolution = "rejected"
    db.flush()

    gname = (row.guest_name or "").strip() or "Guest"
    note = ZoneMessageEvent(
        zone_id=zid,
        sender_id=acting_owner.id,
        guest_access_session_id=row.id,
        type=CanonicalMessageType.PERMISSION.value,
        category=type_category(CanonicalMessageType.PERMISSION),
        scope=type_scope(CanonicalMessageType.PERMISSION),
        text=f"Guest access denied for {gname}.",
        body_json={
            "guest_id": guest_id,
            "guest_name": gname,
            "guest_request_id": row.id,
            "zone_id": zid,
            "resolution": "REJECTED",
        },
        metadata_json={
            "flow": "guest_access_reject",
            "domain_event": "guest_request_rejected",
            "acting_owner_id": acting_owner.id,
            "guest_access_session_db_id": row.id,
        },
    )
    db.add(note)
    db.flush()

    return {
        "ok": True,
        "guest_response": {
            "status": "REJECTED",
            "message": "Access was not approved.",
            "guest_id": guest_id,
        },
    }
