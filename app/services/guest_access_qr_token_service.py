"""Create, list, revoke stored guest QR tokens; validate at arrival."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.domain.event_id import event_ids_equivalent
from app.models.guest_access_qr_token_audit import GuestAccessQrTokenAudit
from app.models.guest_access_qr_token import GuestAccessQrToken
from app.models.owner import Owner, OwnerRole

MAX_GUEST_QR_TOKEN_TTL_HOURS = 24 * 365


def _ensure_admin_zone(owner: Owner, zone_id: str) -> dict | None:
    zid = zone_id.strip()
    if owner.zone_id != zid:
        return {
            "error": "FORBIDDEN",
            "message": "You may only manage guest QR tokens for your own zone.",
            "http_status": 403,
        }
    if owner.role != OwnerRole.ADMINISTRATOR:
        return {
            "error": "FORBIDDEN",
            "message": "Administrator role required.",
            "http_status": 403,
        }
    return None


def _audit_qr_token_action(
    db: Session,
    *,
    token_row: GuestAccessQrToken,
    action: str,
    actor_owner_id: int | None,
    reason: str | None = None,
    metadata: dict | None = None,
) -> None:
    audit = GuestAccessQrTokenAudit(
        token_id=token_row.id,
        zone_id=token_row.zone_id,
        action=action,
        actor_owner_id=actor_owner_id,
        reason=(reason or "").strip() or None,
        metadata_json=metadata or None,
    )
    db.add(audit)


def compute_expires_at(
    *,
    expires_at: datetime | None,
    expires_in_hours: float | None,
) -> tuple[datetime | None, dict | None]:
    if expires_at is not None and expires_in_hours is not None:
        return None, {
            "error": "INVALID_PAYLOAD",
            "message": "Provide either expires_at or expires_in_hours, not both.",
            "http_status": 422,
        }
    if expires_at is not None:
        when = expires_at
        if when.tzinfo is not None:
            when = when.replace(tzinfo=None)
        return when, None
    hours = expires_in_hours if expires_in_hours is not None else 168.0
    if hours <= 0 or hours > MAX_GUEST_QR_TOKEN_TTL_HOURS:
        return None, {
            "error": "INVALID_TTL",
            "message": f"expires_in_hours must be between 0 exclusive and {MAX_GUEST_QR_TOKEN_TTL_HOURS}.",
            "http_status": 422,
        }
    return datetime.utcnow() + timedelta(hours=hours), None


def create_guest_qr_token(
    db: Session,
    acting_owner: Owner,
    *,
    zone_id: str,
    expires_at: datetime | None,
    expires_in_hours: float | None,
    event_id: str | None,
    label: str | None,
    max_uses: int | None,
    is_primary: bool = False,
) -> dict:
    err = _ensure_admin_zone(acting_owner, zone_id)
    if err:
        return err

    if is_primary:
        if expires_at is not None or expires_in_hours is not None:
            return {
                "error": "PRIMARY_TOKEN_EXPIRY_NOT_ALLOWED",
                "message": "Primary guest tokens do not support expires_at or expires_in_hours.",
                "http_status": 422,
            }
        existing_primary = (
            db.query(GuestAccessQrToken)
            .filter(
                GuestAccessQrToken.zone_id == zone_id.strip(),
                GuestAccessQrToken.is_primary.is_(True),
                GuestAccessQrToken.revoked_at.is_(None),
            )
            .order_by(GuestAccessQrToken.created_at.desc())
            .first()
        )
        if existing_primary:
            return {"row": existing_primary}
        when = None
    else:
        when, terr = compute_expires_at(expires_at=expires_at, expires_in_hours=expires_in_hours)
        if terr:
            return terr
        assert when is not None
        if when <= datetime.utcnow():
            return {"error": "INVALID_EXPIRY", "message": "Expiry must be in the future.", "http_status": 422}

    raw = secrets.token_urlsafe(32)
    ev = (event_id or "").strip() or None
    lb = (label or "").strip() or None

    row = GuestAccessQrToken(
        token=raw,
        zone_id=zone_id.strip(),
        event_id=ev,
        label=lb,
        created_by_owner_id=acting_owner.id,
        expires_at=when,
        revoked_at=None,
        is_primary=is_primary,
        max_uses=max_uses,
        use_count=0,
    )
    db.add(row)
    db.flush()
    _audit_qr_token_action(
        db,
        token_row=row,
        action="created_primary" if is_primary else "created",
        actor_owner_id=acting_owner.id,
        metadata={"event_id": ev, "max_uses": max_uses},
    )
    return {"row": row}


def list_guest_qr_tokens(
    db: Session,
    acting_owner: Owner,
    *,
    zone_id: str,
    limit: int,
    include_revoked: bool,
) -> dict:
    err = _ensure_admin_zone(acting_owner, zone_id)
    if err:
        return err
    zid = zone_id.strip()
    q = db.query(GuestAccessQrToken).filter(GuestAccessQrToken.zone_id == zid)
    if not include_revoked:
        q = q.filter(GuestAccessQrToken.revoked_at.is_(None))
    rows = q.order_by(GuestAccessQrToken.created_at.desc()).limit(limit).all()
    return {"rows": rows}


def revoke_guest_qr_token(
    db: Session,
    acting_owner: Owner,
    *,
    zone_id: str,
    token_row_id: int,
) -> dict:
    err = _ensure_admin_zone(acting_owner, zone_id)
    if err:
        return err
    row = db.get(GuestAccessQrToken, token_row_id)
    if not row or row.zone_id != zone_id.strip():
        return {"error": "NOT_FOUND", "message": "QR token not found.", "http_status": 404}
    if row.revoked_at is not None:
        return {"error": "INVALID_STATE", "message": "Token already revoked.", "http_status": 422}
    row.revoked_at = datetime.utcnow()
    db.flush()
    _audit_qr_token_action(
        db,
        token_row=row,
        action="revoked",
        actor_owner_id=acting_owner.id,
    )
    return {"row": row}


def get_guest_qr_token_row_admin(
    db: Session,
    acting_owner: Owner,
    *,
    zone_id: str,
    token_row_id: int,
) -> dict:
    err = _ensure_admin_zone(acting_owner, zone_id)
    if err:
        return err
    row = db.get(GuestAccessQrToken, token_row_id)
    if not row or row.zone_id != zone_id.strip():
        return {"error": "NOT_FOUND", "message": "QR token not found.", "http_status": 404}
    return {"row": row}


def lock_guest_qr_token_row(db: Session, secret: str) -> GuestAccessQrToken | None:
    sec = secret.strip()
    if not sec:
        return None
    q = db.query(GuestAccessQrToken).filter(GuestAccessQrToken.token == sec)
    try:
        return q.with_for_update().first()
    except Exception:
        # SQLite / backends without row locking: still validate serially per connection.
        return q.first()


def validate_locked_guest_qr_token(row: GuestAccessQrToken | None) -> dict | None:
    if row is None:
        return {"error": "INVALID_GUEST_TOKEN", "message": "Unknown guest QR token.", "http_status": 404}
    if row.is_revoked():
        return {"error": "TOKEN_REVOKED", "message": "This guest QR link has been revoked.", "http_status": 403}
    if row.is_expired():
        return {"error": "TOKEN_EXPIRED", "message": "This guest QR link has expired.", "http_status": 403}
    if row.is_depleted():
        return {"error": "TOKEN_DEPLETED", "message": "This guest QR link has reached its maximum number of uses.", "http_status": 403}
    return None


def merge_event_id_for_arrival(*, token_event_id: str | None, payload_event_id: str | None) -> tuple[str | None, dict | None]:
    te = (token_event_id or "").strip()
    pe = (payload_event_id or "").strip()
    if te and pe and not event_ids_equivalent(te, pe):
        return None, {
            "error": "EVENT_MISMATCH",
            "message": "event_id does not match the QR token.",
            "http_status": 422,
        }
    resolved = te or pe or None
    return resolved if resolved else None, None


def apply_successful_arrival_use(db: Session, row: GuestAccessQrToken) -> None:
    row.use_count = int(row.use_count or 0) + 1
    row.last_used_at = datetime.utcnow()
    db.flush()


def serialize_guest_qr_token_public(row: GuestAccessQrToken) -> dict:
    return {
        "id": row.id,
        "zone_id": row.zone_id,
        "event_id": row.event_id,
        "label": row.label,
        "expires_at": row.expires_at,
        "is_primary": bool(row.is_primary),
        "revoked_at": row.revoked_at,
        "max_uses": row.max_uses,
        "use_count": row.use_count,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
        "created_by_owner_id": row.created_by_owner_id,
        "token_suffix": row.token[-6:] if len(row.token) >= 6 else row.token,
    }


def get_or_create_primary_guest_qr_token(
    db: Session,
    acting_owner: Owner,
    *,
    zone_id: str,
) -> dict:
    err = _ensure_admin_zone(acting_owner, zone_id)
    if err:
        return err
    zid = zone_id.strip()
    row = (
        db.query(GuestAccessQrToken)
        .filter(
            GuestAccessQrToken.zone_id == zid,
            GuestAccessQrToken.is_primary.is_(True),
            GuestAccessQrToken.revoked_at.is_(None),
        )
        .order_by(GuestAccessQrToken.created_at.desc())
        .first()
    )
    if row:
        return {"row": row}
    return create_guest_qr_token(
        db,
        acting_owner,
        zone_id=zid,
        expires_at=None,
        expires_in_hours=None,
        event_id=None,
        label="Primary guest token",
        max_uses=None,
        is_primary=True,
    )


def rotate_primary_guest_qr_token(
    db: Session,
    acting_owner: Owner,
    *,
    zone_id: str,
    reason: str | None = None,
) -> dict:
    err = _ensure_admin_zone(acting_owner, zone_id)
    if err:
        return err
    zid = zone_id.strip()
    now = datetime.utcnow()
    old_rows = (
        db.query(GuestAccessQrToken)
        .filter(
            GuestAccessQrToken.zone_id == zid,
            GuestAccessQrToken.is_primary.is_(True),
            GuestAccessQrToken.revoked_at.is_(None),
        )
        .all()
    )
    for old in old_rows:
        old.revoked_at = now
        existing_meta = old.label or ""
        if reason:
            old.label = f"{existing_meta} [rotated: {reason.strip()}]".strip()
        _audit_qr_token_action(
            db,
            token_row=old,
            action="rotated_out",
            actor_owner_id=acting_owner.id,
            reason=reason,
        )
    db.flush()
    created = create_guest_qr_token(
        db,
        acting_owner,
        zone_id=zid,
        expires_at=None,
        expires_in_hours=None,
        event_id=None,
        label="Primary guest token",
        max_uses=None,
        is_primary=True,
    )
    return created
