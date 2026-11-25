"""Email a 4-digit code, then let the account holder set a new password."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import get_password_hash, verify_password
from app.models.owner import Owner
from app.models.password_reset import PasswordReset
from app.services.email_service import send_password_reset_email

logger = logging.getLogger(__name__)

RESET_TOKEN_SECONDS = 900
MIN_PASSWORD_LENGTH = 8


def _now() -> datetime:
    return datetime.utcnow()


def _code_secret() -> bytes:
    raw = (settings.SECRET_KEY or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Verification secret is not configured",
        )
    return raw.encode("utf-8")


def _hash_code(email: str, code: str) -> str:
    payload = f"reset:{email.strip().lower()}:{code}".encode("utf-8")
    return hmac.new(_code_secret(), payload, hashlib.sha256).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


def _find_owner(db: Session, email: str) -> Owner | None:
    normalized = email.strip().lower()
    if not normalized:
        return None
    return db.query(Owner).filter(func.lower(Owner.email) == normalized).first()


def _lookup(db: Session, email: str) -> PasswordReset | None:
    normalized = email.strip().lower()
    return db.query(PasswordReset).filter(PasswordReset.email == normalized).first()


def _cooldown_remaining(row: PasswordReset, now: datetime) -> int:
    elapsed = (now - row.last_sent_at).total_seconds()
    return max(0, int(settings.SIGNUP_CODE_RESEND_SECONDS - elapsed))


def _status(row: PasswordReset, *, sent: bool, delivery: str, now: datetime | None = None) -> dict[str, Any]:
    current = now or _now()
    expires_in = max(0, int((row.expires_at - current).total_seconds()))
    return {
        "email": row.email,
        "expires_in_seconds": expires_in,
        "resend_after_seconds": _cooldown_remaining(row, current),
        "sent": sent,
        "delivery": delivery,
    }


def _assert_new_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must be at least 8 characters",
        )


def _issue_code(row: PasswordReset, *, now: datetime) -> str:
    code = _new_code()
    row.code_hash = _hash_code(row.email, code)
    row.reset_token_hash = None
    row.expires_at = now + timedelta(seconds=int(settings.SIGNUP_CODE_TTL_SECONDS))
    row.last_sent_at = now
    row.attempts = 0
    return code


def _deliver(row: PasswordReset, code: str, *, to_email: str) -> dict[str, Any]:
    result = send_password_reset_email(to_email=to_email, code=code)
    sent = bool(result.get("sent"))
    delivery = str(result.get("delivery") or "failed")
    if not sent and delivery != "logged":
        row.last_sent_at = _now() - timedelta(seconds=int(settings.SIGNUP_CODE_RESEND_SECONDS))
    return _status(row, sent=sent, delivery=delivery)


def _require_owner(db: Session, email: str) -> Owner:
    owner = _find_owner(db, email)
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account uses that email.",
        )
    return owner


def start_password_reset(db: Session, email: str) -> dict[str, Any]:
    """Email a 4-digit code when the address belongs to an account."""
    owner = _require_owner(db, email)
    email_key = owner.email.strip().lower()
    now = _now()
    row = _lookup(db, email_key)
    if row is None:
        row = PasswordReset(
            email=email_key,
            code_hash="",
            reset_token_hash=None,
            expires_at=now,
            last_sent_at=now,
            attempts=0,
            created_at=now,
        )
        db.add(row)
    elif row.expires_at > now and row.code_hash and _cooldown_remaining(row, now) > 0:
        db.flush()
        return _status(row, sent=False, delivery="cooldown", now=now)

    code = _issue_code(row, now=now)
    db.flush()
    return _deliver(row, code, to_email=owner.email)


def resend_password_reset(db: Session, email: str) -> dict[str, Any]:
    owner = _require_owner(db, email)
    row = _lookup(db, owner.email)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request a reset code first.",
        )
    now = _now()
    if row.expires_at > now and row.code_hash and _cooldown_remaining(row, now) > 0:
        return _status(row, sent=False, delivery="cooldown", now=now)
    code = _issue_code(row, now=now)
    db.flush()
    return _deliver(row, code, to_email=owner.email)


def confirm_password_reset(db: Session, email: str, code: str) -> dict[str, Any]:
    """Check the emailed code and return a single-use reset token."""
    owner = _require_owner(db, email)
    row = _lookup(db, owner.email)
    if row is None or not row.code_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request a reset code first.",
        )
    now = _now()
    if row.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That code has expired. Request a new one.",
        )
    if row.attempts >= int(settings.SIGNUP_CODE_MAX_ATTEMPTS):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Request a new code.",
        )

    submitted = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(submitted) != 4 or not hmac.compare_digest(
        _hash_code(row.email, submitted),
        row.code_hash,
    ):
        row.attempts += 1
        db.commit()
        remaining = max(0, int(settings.SIGNUP_CODE_MAX_ATTEMPTS) - row.attempts)
        if remaining == 0:
            detail = "Too many attempts. Request a new code."
            code_status = status.HTTP_429_TOO_MANY_REQUESTS
        else:
            detail = f"Incorrect code. {remaining} attempts left."
            code_status = status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code_status, detail=detail)

    token = secrets.token_urlsafe(32)
    row.code_hash = ""
    row.reset_token_hash = _hash_token(token)
    row.expires_at = now + timedelta(seconds=RESET_TOKEN_SECONDS)
    row.attempts = 0
    db.flush()
    return {
        "email": row.email,
        "reset_token": token,
        "expires_in_seconds": RESET_TOKEN_SECONDS,
    }


def complete_password_reset(
    db: Session,
    email: str,
    reset_token: str,
    new_password: str,
) -> dict[str, Any]:
    """Set a new password when the reset token from the code step is valid."""
    _assert_new_password(new_password)
    owner = _require_owner(db, email)
    row = _lookup(db, owner.email)
    token = (reset_token or "").strip()
    if row is None or not row.reset_token_hash or not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verify the email code before choosing a new password.",
        )
    if row.expires_at <= _now() or not hmac.compare_digest(
        _hash_token(token),
        row.reset_token_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That reset session has expired. Request a new code.",
        )
    if verify_password(new_password, owner.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose a password that is different from your current one.",
        )
    owner.hashed_password = get_password_hash(new_password)
    db.delete(row)
    db.flush()
    logger.info("Password reset for %s", owner.email)
    return {"updated": True, "email": owner.email}


def change_password(
    db: Session,
    owner: Owner,
    current_password: str,
    new_password: str,
) -> dict[str, Any]:
    """Replace the password for a signed-in account."""
    if not verify_password(current_password or "", owner.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )
    _assert_new_password(new_password)
    if verify_password(new_password, owner.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose a password that is different from your current one.",
        )
    owner.hashed_password = get_password_hash(new_password)
    db.flush()
    logger.info("Password changed for owner %s", owner.id)
    return {"updated": True}
