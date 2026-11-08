"""Public username helpers for owners.

Usernames are the public handle shown instead of private login emails.
Allowed characters: lowercase letters, digits, underscore.
Auto-generated style: ``firstname`` + ``lastname`` + sequential number
starting at 1 (e.g. ``johndoe1``, then ``johndoe2`` if taken).
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.owner import Owner

USERNAME_MIN_LEN = 3
USERNAME_MAX_LEN = 64
USERNAME_PATTERN = re.compile(r"^[a-z0-9_]+$")
USERNAME_INFO_MESSAGE = (
    "Username may only contain lowercase letters, numbers, and underscores."
)


def sanitize_name_part(value: str | None) -> str:
    """Lowercase letters/digits only from a name fragment."""
    raw = (value or "").strip().lower()
    return re.sub(r"[^a-z0-9]", "", raw)


def username_base_from_names(first_name: str | None, last_name: str | None) -> str:
    """Build the alphabetic base used before a uniqueness suffix."""
    first = sanitize_name_part(first_name)
    last = sanitize_name_part(last_name)
    base = f"{first}{last}".strip("_")
    if not base:
        base = "user"
    # Leave room for a reasonably large numeric suffix.
    return base[: max(1, USERNAME_MAX_LEN - 6)]


def validate_username_format(username: str) -> str | None:
    """Return an error message when the username is invalid, else ``None``."""
    value = (username or "").strip()
    if not value:
        return "Username is required."
    if value != value.lower():
        return USERNAME_INFO_MESSAGE
    if not USERNAME_PATTERN.fullmatch(value):
        return USERNAME_INFO_MESSAGE
    if len(value) < USERNAME_MIN_LEN:
        return f"Username must be at least {USERNAME_MIN_LEN} characters."
    if len(value) > USERNAME_MAX_LEN:
        return f"Username must be at most {USERNAME_MAX_LEN} characters."
    return None


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def get_owner_by_username(db: Session, username: str) -> Optional[Owner]:
    normalized = normalize_username(username)
    if not normalized:
        return None
    return (
        db.query(Owner)
        .filter(func.lower(Owner.username) == normalized)
        .first()
    )


def is_username_available(
    db: Session,
    username: str,
    *,
    exclude_owner_id: int | None = None,
) -> bool:
    existing = get_owner_by_username(db, username)
    if existing is None:
        return True
    if exclude_owner_id is not None and int(existing.id) == int(exclude_owner_id):
        return True
    return False


def generate_unique_username(
    db: Session,
    first_name: str | None,
    last_name: str | None,
    *,
    exclude_owner_id: int | None = None,
) -> str:
    """Allocate ``firstname`` + ``lastname`` + N unique across owners.

    Tries ``johndoe1``, then ``johndoe2``, and so on until an unused value is found.
    """
    base = username_base_from_names(first_name, last_name)
    for n in range(1, 100_000):
        candidate = f"{base}{n}"[:USERNAME_MAX_LEN]
        if validate_username_format(candidate):
            continue
        if is_username_available(db, candidate, exclude_owner_id=exclude_owner_id):
            return candidate
    # Extremely unlikely fallback.
    for n in range(1, 100_000):
        candidate = f"user{n}"
        if is_username_available(db, candidate, exclude_owner_id=exclude_owner_id):
            return candidate
    raise RuntimeError("Unable to allocate a unique username")


def ensure_owner_username(db: Session, owner: Owner) -> str:
    """Ensure ``owner.username`` is set; generate one when missing."""
    current = normalize_username(getattr(owner, "username", None) or "")
    if current and not validate_username_format(current):
        if is_username_available(db, current, exclude_owner_id=int(owner.id)):
            owner.username = current
            return current
    generated = generate_unique_username(
        db,
        getattr(owner, "first_name", None),
        getattr(owner, "last_name", None),
        exclude_owner_id=int(owner.id) if getattr(owner, "id", None) else None,
    )
    owner.username = generated
    return generated


def backfill_missing_usernames(db: Session) -> int:
    """Assign usernames to owners that still lack one. Returns updated count."""
    rows = (
        db.query(Owner)
        .filter((Owner.username.is_(None)) | (Owner.username == ""))
        .order_by(Owner.id.asc())
        .all()
    )
    updated = 0
    for owner in rows:
        ensure_owner_username(db, owner)
        updated += 1
    if updated:
        db.commit()
    return updated
