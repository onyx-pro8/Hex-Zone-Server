"""CRUD for public registration codes."""
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.security import generate_registration_code_token
from app.models import RegistrationCode


def create_registration_code(
    db: Session,
    *,
    expires_in_hours: int | None = None,
    code: str | None = None,
    expires_at: datetime | None = None,
    email: str | None = None,
    pricing_tier: str | None = None,
    tier_level: int | None = None,
    api_key: str | None = None,
) -> RegistrationCode:
    """Persist a new single-use registration code."""
    if code:
        stripped = code.strip()
        # HMAC REG-CODE format; random legacy tokens stay as minted.
        token = (
            stripped.upper().replace(" ", "")
            if len(stripped) <= 13 and "-" in stripped
            else stripped
        )
    else:
        token = generate_registration_code_token()
    if expires_at is None:
        hours = expires_in_hours if expires_in_hours is not None else 24
        expires_at = datetime.utcnow() + timedelta(hours=hours)
    row = RegistrationCode(
        code=token,
        expires_at=expires_at,
        email=email,
        pricing_tier=pricing_tier,
        tier_level=tier_level,
        api_key=api_key,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    return row


def get_registration_code(db: Session, code: str) -> RegistrationCode | None:
    token = code.strip().upper().replace(" ", "")
    return db.execute(
        select(RegistrationCode).where(RegistrationCode.code == token)
    ).scalars().first()


def refresh_registration_code_issuance(
    db: Session,
    row: RegistrationCode,
    *,
    expires_at: datetime,
    email: str,
    pricing_tier: str,
    tier_level: int | None,
    api_key: str,
) -> RegistrationCode:
    """Re-bind a deterministic HMAC code to a fresh api_key and expiry."""
    row.expires_at = expires_at
    row.email = email
    row.pricing_tier = pricing_tier
    row.tier_level = tier_level
    row.api_key = api_key
    row.revoked = False
    row.used = False
    db.flush()
    db.refresh(row)
    return row


def list_active_codes_for_email(db: Session, email: str) -> list[RegistrationCode]:
    """Unused, non-revoked, non-expired issuance rows for an email (newest first)."""
    now = datetime.utcnow()
    result = db.execute(
        select(RegistrationCode)
        .where(
            RegistrationCode.email == email,
            RegistrationCode.used.is_(False),
            RegistrationCode.revoked.is_(False),
            RegistrationCode.expires_at > now,
        )
        .order_by(RegistrationCode.created_at.desc())
    )
    return list(result.scalars().all())


def revoke_pending_issuances_for_email_tier(
    db: Session,
    email: str,
    pricing_tier: str,
    tier_level: int | None,
) -> None:
    """Revoke unused issuance rows so a new api_key can be bound to the same HMAC code."""
    now = datetime.utcnow()
    tier_filter = (
        RegistrationCode.tier_level.is_(None)
        if tier_level is None
        else RegistrationCode.tier_level == tier_level
    )
    rows = db.execute(
        select(RegistrationCode).where(
            RegistrationCode.email == email,
            RegistrationCode.pricing_tier == pricing_tier,
            tier_filter,
            RegistrationCode.used.is_(False),
            RegistrationCode.revoked.is_(False),
            RegistrationCode.expires_at > now,
        )
    ).scalars().all()
    for row in rows:
        row.revoked = True
    if rows:
        db.flush()


def try_consume_registration_code(db: Session, code: str) -> bool:
    """Atomically mark a valid code as used. Returns True if one row was updated."""
    token = code.strip().upper().replace(" ", "")
    now = datetime.utcnow()
    result = db.execute(
        update(RegistrationCode)
        .where(
            RegistrationCode.code == token,
            RegistrationCode.used.is_(False),
            RegistrationCode.revoked.is_(False),
            RegistrationCode.expires_at > now,
        )
        .values(used=True)
    )
    return result.rowcount == 1
