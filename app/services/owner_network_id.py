"""Allocate and backfill per-user network identifiers."""
from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from app.models import Owner


def generate_network_id() -> str:
    """Generate a human-readable network id for a new owner/user."""
    return f"NET-{secrets.token_hex(3).upper()}"


def ensure_owner_network_id(db: Session, owner: Owner) -> str:
    """Return the owner's network id, allocating one when missing."""
    existing = (owner.network_id or "").strip()
    if existing:
        return existing
    for _ in range(20):
        candidate = generate_network_id()
        clash = (
            db.query(Owner.id)
            .filter(Owner.network_id == candidate)
            .first()
        )
        if clash is None:
            owner.network_id = candidate
            db.flush()
            return candidate
    raise RuntimeError(f"Could not allocate unique network_id for owner {owner.id}")


def backfill_missing_network_ids(db: Session) -> int:
    """Assign network ids to legacy owners. Returns rows updated."""
    rows = (
        db.query(Owner)
        .filter((Owner.network_id.is_(None)) | (Owner.network_id == ""))
        .order_by(Owner.id.asc())
        .all()
    )
    for owner in rows:
        ensure_owner_network_id(db, owner)
    if rows:
        db.commit()
    return len(rows)
