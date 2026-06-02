"""Registration codes for administrator self-registration (setup wizard)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import generate_api_key
from app.crud import owner as owner_crud
from app.crud import registration_code as registration_code_crud
from app.services.email_service import send_registration_code_email, support_contact_dict

# Stateless tier code accepted on POST without a matching DB row (product / dev convenience).
STATIC_ADMIN_REGISTRATION_TIERS: frozenset[str] = frozenset({"FREE"})

# Canonical pricing tier keys (normalized lowercase snake_case).
PRICING_TIER_PRIVATE = "private"
PRICING_TIER_PRIVATE_PLUS = "private_plus"
PRICING_TIER_EXCLUSIVE = "exclusive"
PRICING_TIER_ENHANCED = "enhanced"
PRICING_TIER_ENHANCED_PLUS = "enhanced_plus"

VALID_PRICING_TIERS: frozenset[str] = frozenset(
    {
        PRICING_TIER_PRIVATE,
        PRICING_TIER_PRIVATE_PLUS,
        PRICING_TIER_EXCLUSIVE,
        PRICING_TIER_ENHANCED,
        PRICING_TIER_ENHANCED_PLUS,
    }
)

ENHANCED_PLUS_LEVELS: dict[int, int] = {
    1: 20,
    2: 50,
    3: 100,
    4: 500,
    5: 2500,
}

PRICING_TIER_LABELS: dict[str, str] = {
    PRICING_TIER_PRIVATE: "Private — unlimited users (system owner)",
    PRICING_TIER_PRIVATE_PLUS: "Private Plus [+] — max 10 users (family)",
    PRICING_TIER_EXCLUSIVE: "Exclusive — 1 user, 3 zones, 1 device (FREE/ads)",
    PRICING_TIER_ENHANCED: "Enhanced — 1 user, 3 zones, 1 device",
    PRICING_TIER_ENHANCED_PLUS: "Enhanced Plus [+] — tiered user capacity",
}

logger = logging.getLogger(__name__)

_REG_CODE_FORMAT = re.compile(r"^[A-F0-9]{6}-[A-F0-9]{6}$")


def normalize_registration_code(code: str) -> str:
    """Canonical form stored in DB and compared at verification time."""
    return code.strip().upper().replace(" ", "")


@dataclass(frozen=True)
class PricingTierSpec:
    pricing_tier: str
    tier_level: int | None
    price_tier_key: str
    label: str


def _hmac_secret_bytes() -> bytes:
    raw = (settings.REGISTRATION_CODE_HMAC_SECRET or settings.SECRET_KEY or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Registration code secret is not configured",
        )
    return raw.encode("utf-8")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_pricing_tier_key(pricing_tier: str) -> str:
    key = pricing_tier.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "privateplus": "private_plus",
        "private+": "private_plus",
        "enhancedplus": "enhanced_plus",
        "enhanced+": "enhanced_plus",
        "enhance_plus": "enhanced_plus",
        "enhance+": "enhanced_plus",
    }
    return aliases.get(key, key)


def resolve_pricing_tier_spec(
    pricing_tier: str,
    tier_level: int | None = None,
) -> PricingTierSpec:
    key = normalize_pricing_tier_key(pricing_tier)
    if key not in VALID_PRICING_TIERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Invalid pricing_tier. Expected one of: "
                "private, private_plus, exclusive, enhanced, enhanced_plus"
            ),
        )

    level: int | None = None
    if key == PRICING_TIER_ENHANCED_PLUS:
        if tier_level is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="tier_level (1–5) is required for enhanced_plus pricing tier",
            )
        try:
            level = int(tier_level)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="tier_level must be an integer between 1 and 5",
            ) from exc
        if level not in ENHANCED_PLUS_LEVELS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="tier_level must be between 1 and 5 for enhanced_plus",
            )
        max_users = ENHANCED_PLUS_LEVELS[level]
        label = f"Enhanced Plus [+] Level {level} — max {max_users} users"
        price_tier_key = f"enhanced_plus_{level}"
    else:
        label = PRICING_TIER_LABELS[key]
        price_tier_key = key

    return PricingTierSpec(
        pricing_tier=key,
        tier_level=level,
        price_tier_key=price_tier_key,
        label=label,
    )


def generate_registration_code(email: str, price_tier: str, secret_key: bytes) -> str:
    """
    Generate a secure, deterministic verification code based on email and pricing tier.

    Uses HMAC-SHA256; truncates to a readable XXXXXX-XXXXXX code.
    """
    normalized_email = normalize_email(email)
    normalized_tier = price_tier.strip().lower()
    payload = f"{normalized_email}:{normalized_tier}".encode("utf-8")
    signature = hmac.new(secret_key, payload, hashlib.sha256).hexdigest()
    raw_code = signature[:12].upper()
    return f"{raw_code[:6]}-{raw_code[6:]}"


def verify_registration_code_hmac(
    provided_code: str,
    email: str,
    price_tier: str,
    secret_key: bytes,
) -> bool:
    expected = generate_registration_code(email, price_tier, secret_key)
    return hmac.compare_digest(
        normalize_registration_code(provided_code), expected
    )


def _format_expires_at(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


def mint_registration_code(db: Session) -> str:
    """Create a single-use DB-backed code and return plaintext; fallback to FREE on DB failure."""
    try:
        row = registration_code_crud.create_registration_code(
            db,
            expires_in_hours=settings.REGISTRATION_CODE_EXPIRE_HOURS,
        )
        return row.code
    except Exception as exc:
        db.rollback()
        logger.exception(
            "Falling back to static admin registration tier because code mint failed: %s",
            exc,
        )
        return "FREE"


def _ensure_registration_code_schema(db: Session) -> None:
    """Best-effort: add the email/pricing_tier/tier_level/api_key columns inline.

    Lifespan startup already runs `patch_registration_code_email_columns`, but if a
    transient failure occurred there (e.g. lock_timeout during a rolling deploy),
    requests would otherwise 500 with `UndefinedColumn` until the next process
    restart. This function self-heals on first request after the lock clears.
    """
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    try:
        from app.database import patch_registration_code_email_columns
        patch_registration_code_email_columns()
    except Exception as exc:
        logger.warning("Inline registration_codes schema self-heal failed: %s", exc)


def issue_registration_code_for_email_tier(
    db: Session,
    *,
    email: str,
    pricing_tier: str,
    tier_level: int | None = None,
) -> dict[str, Any]:
    """
    Issue an HMAC registration code for administrator email + pricing tier.

    Persists issuance metadata (email, tier, pre-allocated api_key) for single-use
    consumption at POST /owners/register. Sends confirmation email when SMTP is configured.
    """
    normalized_email = normalize_email(email)
    if owner_crud.get_owner_by_email(db, normalized_email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    spec = resolve_pricing_tier_spec(pricing_tier, tier_level)
    secret = _hmac_secret_bytes()
    code = generate_registration_code(normalized_email, spec.price_tier_key, secret)
    api_key = generate_api_key()
    expires_at = datetime.utcnow() + timedelta(hours=settings.REGISTRATION_CODE_EXPIRE_HOURS)

    try:
        registration_code_crud.revoke_pending_issuances_for_email_tier(
            db,
            normalized_email,
            spec.pricing_tier,
            spec.tier_level,
        )

        # HMAC codes are deterministic — re-issue must update the existing row, not INSERT
        # another row with the same `code` (unique constraint).
        existing = registration_code_crud.get_registration_code(db, code)
        if existing:
            if existing.used:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This registration code has already been used",
                )
            row = registration_code_crud.refresh_registration_code_issuance(
                db,
                existing,
                expires_at=expires_at,
                email=normalized_email,
                pricing_tier=spec.pricing_tier,
                tier_level=spec.tier_level,
                api_key=api_key,
            )
        else:
            row = registration_code_crud.create_registration_code(
                db,
                code=code,
                expires_at=expires_at,
                email=normalized_email,
                pricing_tier=spec.pricing_tier,
                tier_level=spec.tier_level,
                api_key=api_key,
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Most likely an `UndefinedColumn` because the per-issuance schema patch did
        # not apply yet on the deployed Postgres. Self-heal once and retry. If the
        # second attempt still fails, surface a clean 503 (CORS-aware) instead of a
        # raw 500 that the browser logs as a CORS error.
        logger.warning(
            "First-pass registration_codes write failed (%s); attempting schema self-heal",
            exc,
        )
        db.rollback()
        _ensure_registration_code_schema(db)
        try:
            registration_code_crud.revoke_pending_issuances_for_email_tier(
                db,
                normalized_email,
                spec.pricing_tier,
                spec.tier_level,
            )
            existing = registration_code_crud.get_registration_code(db, code)
            if existing:
                if existing.used:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="This registration code has already been used",
                    )
                row = registration_code_crud.refresh_registration_code_issuance(
                    db,
                    existing,
                    expires_at=expires_at,
                    email=normalized_email,
                    pricing_tier=spec.pricing_tier,
                    tier_level=spec.tier_level,
                    api_key=api_key,
                )
            else:
                row = registration_code_crud.create_registration_code(
                    db,
                    code=code,
                    expires_at=expires_at,
                    email=normalized_email,
                    pricing_tier=spec.pricing_tier,
                    tier_level=spec.tier_level,
                    api_key=api_key,
                )
        except HTTPException:
            raise
        except Exception as retry_exc:
            db.rollback()
            logger.exception(
                "Registration code issuance still failing after schema self-heal: %s",
                retry_exc,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Registration code service is initializing. Please try again in a minute."
                ),
            ) from retry_exc

    expires_iso = _format_expires_at(row.expires_at)

    email_result = send_registration_code_email(
        to_email=normalized_email,
        registration_code=code,
        api_key=api_key,
        pricing_tier_label=spec.label,
        expires_at_iso=expires_iso,
        contact=support_contact_dict(),
    )

    return {
        "registration_code": code,
        "api_key": api_key,
        "pricing_tier": spec.pricing_tier,
        "tier_level": spec.tier_level,
        "pricing_tier_label": spec.label,
        "expires_at": expires_iso,
        "email": normalized_email,
        "contact": support_contact_dict(),
        "email_delivery": email_result,
    }


def _try_consume_hmac_registration_row(
    db: Session,
    code: str,
    *,
    registration_email: str | None = None,
) -> bool:
    """Validate HMAC + atomically consume a matching issuance row."""
    row = registration_code_crud.get_registration_code(db, code)
    if not row or row.used or row.revoked or row.is_expired():
        return False
    if not row.email or not row.pricing_tier:
        return False

    spec = resolve_pricing_tier_spec(row.pricing_tier, row.tier_level)
    secret = _hmac_secret_bytes()
    if not verify_registration_code_hmac(code, row.email, spec.price_tier_key, secret):
        return False

    if registration_email:
        if normalize_email(registration_email) != normalize_email(row.email):
            return False

    return registration_code_crud.try_consume_registration_code(db, code)


def _assert_account_type_matches_issuance(
    row,
    *,
    account_type: str | None,
) -> None:
    if not row or not row.pricing_tier or not account_type:
        return
    expected = normalize_pricing_tier_key(account_type)
    if expected != row.pricing_tier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Account type does not match the pricing tier used to issue this registration code. "
                f"Expected {row.pricing_tier}, got {expected}."
            ),
        )


def require_and_consume_admin_registration_code(
    db: Session,
    code: str | None,
    *,
    registration_email: str | None = None,
    account_type: str | None = None,
) -> str | None:
    """
    Administrators self-registering must provide a valid code.

    Returns the pre-allocated api_key from the issuance row when present.
    """
    if code is None or not str(code).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Registration code is required for administrator self-registration. "
                "Request one via POST /utils/registration-code/issue with your email and "
                "pricing tier, or use tier code FREE when permitted."
            ),
        )
    normalized = normalize_registration_code(str(code))

    if normalized in STATIC_ADMIN_REGISTRATION_TIERS:
        return None

    if _REG_CODE_FORMAT.match(normalized):
        row = registration_code_crud.get_registration_code(db, normalized)
        if row and row.api_key and not row.used and not row.revoked and not row.is_expired():
            _assert_account_type_matches_issuance(row, account_type=account_type)
            if _try_consume_hmac_registration_row(
                db, normalized, registration_email=registration_email
            ):
                return row.api_key
        if registration_email:
            for spec_key in _candidate_price_tier_keys_for_email(db, registration_email):
                secret = _hmac_secret_bytes()
                if verify_registration_code_hmac(normalized, registration_email, spec_key, secret):
                    row_match = registration_code_crud.get_registration_code(db, normalized)
                    _assert_account_type_matches_issuance(
                        row_match, account_type=account_type
                    )
                    if _try_consume_hmac_registration_row(
                        db, normalized, registration_email=registration_email
                    ):
                        row_after = registration_code_crud.get_registration_code(db, normalized)
                        return row_after.api_key if row_after else None

    if registration_code_crud.try_consume_registration_code(db, normalized):
        row = registration_code_crud.get_registration_code(db, normalized)
        return row.api_key if row and row.api_key else None

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid, expired, revoked, or already used registration code",
    )


def _candidate_price_tier_keys_for_email(db: Session, email: str) -> list[str]:
    """Recent issuance rows for an email — used to re-validate HMAC at register time."""
    rows = registration_code_crud.list_active_codes_for_email(db, normalize_email(email))
    keys: list[str] = []
    for row in rows:
        if not row.pricing_tier:
            continue
        try:
            spec = resolve_pricing_tier_spec(row.pricing_tier, row.tier_level)
            if spec.price_tier_key not in keys:
                keys.append(spec.price_tier_key)
        except HTTPException:
            continue
    return keys
