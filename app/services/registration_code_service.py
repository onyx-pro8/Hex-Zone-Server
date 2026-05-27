"""Registration codes for administrator self-registration (setup wizard)."""
from __future__ import annotations

import logging
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.crud import registration_code as registration_code_crud


# Stateless tier code accepted on POST without a matching DB row (product / dev convenience).
STATIC_ADMIN_REGISTRATION_TIERS: frozenset[str] = frozenset({"FREE"})
logger = logging.getLogger(__name__)


def mint_registration_code(db: Session) -> str:
    """Create a single-use DB-backed code and return plaintext; fallback to FREE on DB failure."""
    try:
        row = registration_code_crud.create_registration_code(
            db,
            expires_in_hours=settings.REGISTRATION_CODE_EXPIRE_HOURS,
        )
        return row.code
    except Exception as exc:
        # Keep onboarding available during transient DB outages. FREE is already
        # accepted by registration validators as a static tier code.
        db.rollback()
        logger.exception(
            "Falling back to static admin registration tier because code mint failed: %s",
            exc,
        )
        return "FREE"


def require_and_consume_admin_registration_code(db: Session, code: str | None) -> None:
    """Administrators self-registering must echo a valid minted code or tier code FREE."""
    if code is None or not str(code).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Registration code is required for administrator self-registration. "
                "Fetch a code from GET /utils/registration-code (or GET /owners/registration-code) "
                "or use tier code FREE when permitted."
            ),
        )
    normalized = str(code).strip()
    if normalized.upper() in STATIC_ADMIN_REGISTRATION_TIERS:
        return
    if registration_code_crud.try_consume_registration_code(db, normalized):
        return
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid, expired, revoked, or already used registration code",
    )
