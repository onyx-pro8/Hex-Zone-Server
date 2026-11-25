"""Hold a public signup until the 4-digit Resend email code is confirmed."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import get_password_hash
from app.models.owner import Owner
from app.models.pending_signup import PendingSignup
from app.services.auth_service import register_user
from app.services.email_service import send_signup_verification_email

logger = logging.getLogger(__name__)


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
    payload = f"{email.strip().lower()}:{code}".encode("utf-8")
    return hmac.new(_code_secret(), payload, hashlib.sha256).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


def _cooldown_remaining(row: PendingSignup, now: datetime) -> int:
    elapsed = (now - row.last_sent_at).total_seconds()
    return max(0, int(settings.SIGNUP_CODE_RESEND_SECONDS - elapsed))


def _status(row: PendingSignup, *, sent: bool, delivery: str, now: datetime | None = None) -> dict[str, Any]:
    current = now or _now()
    expires_in = max(0, int((row.expires_at - current).total_seconds()))
    return {
        "email": row.email,
        "expires_in_seconds": expires_in,
        "resend_after_seconds": _cooldown_remaining(row, current),
        "sent": sent,
        "delivery": delivery,
    }


def _registration_payload(payload: dict) -> tuple[dict[str, Any], str]:
    stored = dict(payload)
    password = str(stored.pop("password", "") or "")
    if len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must be at least 8 characters",
        )
    email = str(stored.get("email") or "").strip()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Email is required",
        )
    stored["email"] = email
    return stored, password


def _assert_email_available(db: Session, email: str) -> None:
    existing = db.query(Owner).filter(Owner.email == email).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )


def _assert_communal_selectable(db: Session, payload: dict) -> None:
    account_type = str(payload.get("accountType") or "").strip().lower()
    raw_communal = payload.get("communalId") or payload.get("communal_id")
    if account_type != "exclusive" or not str(raw_communal or "").strip():
        return
    from app.services.individual_communal_signup import (
        assert_exclusive_communal_id_selectable,
    )

    assert_exclusive_communal_id_selectable(db, raw_communal)


def _apply_payload(row: PendingSignup, stored: dict[str, Any], password: str) -> None:
    row.payload_json = json.dumps(stored)
    row.password_hash = get_password_hash(password)


def _lookup(db: Session, email: str) -> PendingSignup | None:
    normalized = email.strip().lower()
    return db.query(PendingSignup).filter(PendingSignup.email == normalized).first()


def _issue_code(row: PendingSignup, *, now: datetime) -> str:
    code = _new_code()
    row.code_hash = _hash_code(row.email, code)
    row.expires_at = now + timedelta(seconds=int(settings.SIGNUP_CODE_TTL_SECONDS))
    row.last_sent_at = now
    row.attempts = 0
    return code


def _deliver(row: PendingSignup, code: str, *, to_email: str) -> dict[str, Any]:
    result = send_signup_verification_email(to_email=to_email, code=code)
    sent = bool(result.get("sent"))
    delivery = str(result.get("delivery") or "failed")
    if not sent and delivery != "logged":
        # Allow an immediate resend when Resend rejects the message.
        row.last_sent_at = _now() - timedelta(seconds=int(settings.SIGNUP_CODE_RESEND_SECONDS))
    return _status(row, sent=sent, delivery=delivery)


def start_signup_verification(db: Session, payload: dict) -> dict[str, Any]:
    """Validate the signup, store it, and email a 4-digit code."""
    stored, password = _registration_payload(payload)
    email = stored["email"]
    email_key = email.lower()
    _assert_email_available(db, email)
    _assert_communal_selectable(db, stored)

    now = _now()
    row = _lookup(db, email_key)
    if row is None:
        row = PendingSignup(
            email=email_key,
            code_hash="",
            payload_json="{}",
            password_hash="",
            expires_at=now,
            last_sent_at=now,
            attempts=0,
            created_at=now,
        )
        db.add(row)
    else:
        _apply_payload(row, stored, password)
        if row.expires_at > now and _cooldown_remaining(row, now) > 0:
            db.flush()
            return _status(row, sent=False, delivery="cooldown", now=now)

    _apply_payload(row, stored, password)
    code = _issue_code(row, now=now)
    db.flush()
    return _deliver(row, code, to_email=email)


def resend_signup_verification(db: Session, email: str) -> dict[str, Any]:
    row = _lookup(db, email)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request a verification code from the create-account page first.",
        )
    now = _now()
    if row.expires_at > now and _cooldown_remaining(row, now) > 0:
        return _status(row, sent=False, delivery="cooldown", now=now)
    code = _issue_code(row, now=now)
    db.flush()
    return _deliver(row, code, to_email=row.email)


def confirm_signup_verification(db: Session, email: str, code: str) -> dict[str, Any]:
    """Create the account when the emailed code matches."""
    row = _lookup(db, email)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request a verification code from the create-account page first.",
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

    try:
        stored = json.loads(row.payload_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification expired. Create the account again.",
        ) from exc
    if not isinstance(stored, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification expired. Create the account again.",
        )
    created = register_user(db, stored, hashed_password=row.password_hash)
    db.delete(row)
    db.flush()
    return created
