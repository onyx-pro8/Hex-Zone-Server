"""CRUD operations for QR Registration."""
from sqlalchemy.orm import Session
from sqlalchemy.future import select
from app.models import QRRegistration
from app.core.security import generate_qr_token
from datetime import datetime, timedelta
from typing import Optional


def create_qr_registration(
    db: Session,
    owner_id: int,
    expires_in_hours: int | None = 24,
) -> QRRegistration:
    """Create a new QR registration token.

    ``expires_in_hours=None`` (or ``0``) means the token never expires.
    Every invite token is single-use (one successful join).
    Also mints a Communal ID for the future Individual invitee.
    """
    from app.services.communal_zone_service import mint_invite_communal_id

    token = generate_qr_token()
    if expires_in_hours is None or int(expires_in_hours) <= 0:
        expires_at = None
    else:
        expires_at = datetime.utcnow() + timedelta(hours=int(expires_in_hours))

    db_qr = QRRegistration(
        token=token,
        owner_id=owner_id,
        expires_at=expires_at,
        communal_id=mint_invite_communal_id(db),
    )
    db.add(db_qr)
    db.flush()
    db.refresh(db_qr)
    return db_qr


def ensure_qr_communal_id(db: Session, qr: QRRegistration) -> str:
    """Backfill a Communal ID on a legacy invite that predates this column."""
    from app.services.communal_zone_service import mint_invite_communal_id

    existing = (getattr(qr, "communal_id", None) or "").strip()
    if existing:
        return existing.upper()
    qr.communal_id = mint_invite_communal_id(db)
    db.flush()
    return qr.communal_id


def get_qr_registration(db: Session, token: str) -> Optional[QRRegistration]:
    """Get a QR registration by token."""
    result = db.execute(
        select(QRRegistration).where(QRRegistration.token == token)
    )
    return result.scalars().first()


def mark_qr_registration_used(db: Session, token: str) -> Optional[QRRegistration]:
    """Mark a QR registration as used."""
    qr = get_qr_registration(db, token)
    if not qr:
        return None
    
    qr.used = True
    db.flush()
    db.refresh(qr)
    return qr


def list_qr_registrations(
    db: Session,
    owner_id: int,
    skip: int = 0,
    limit: int = 100,
) -> list:
    """List QR registrations for an owner."""
    result = db.execute(
        select(QRRegistration)
        .where(QRRegistration.owner_id == owner_id)
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()
