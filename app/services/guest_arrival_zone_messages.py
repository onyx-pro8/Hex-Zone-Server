"""Per-zone guest arrival copy: defaults, DB overrides, and admin upsert."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.guest_access_zone_message import GuestAccessZoneMessage

DEFAULT_EXPECTED_ARRIVAL_MESSAGE = "You are expected. Please proceed."
DEFAULT_UNEXPECTED_ARRIVAL_MESSAGE = "You are not scheduled. Please wait for approval."
DEFAULT_GUEST_PASS_VERIFIED_MESSAGE = "You are expected. Guest pass verified."

MAX_GUEST_ARRIVAL_MESSAGE_LEN = 500


@dataclass(frozen=True)
class ResolvedGuestArrivalMessages:
    """Effective guest-facing strings for a zone (defaults merged with nullable overrides)."""

    expected_arrival_message: str
    unexpected_arrival_message: str
    guest_pass_verified_message: str

    def defaults_dict(self) -> dict[str, str]:
        return {
            "expected_arrival_message": DEFAULT_EXPECTED_ARRIVAL_MESSAGE,
            "unexpected_arrival_message": DEFAULT_UNEXPECTED_ARRIVAL_MESSAGE,
            "guest_pass_verified_message": DEFAULT_GUEST_PASS_VERIFIED_MESSAGE,
        }


def resolve_guest_arrival_messages(db: Session, zone_id: str) -> ResolvedGuestArrivalMessages:
    """Return built-in defaults when no row exists or a column is null/blank."""
    zid = (zone_id or "").strip()
    row = None
    if zid:
        row = db.query(GuestAccessZoneMessage).filter(GuestAccessZoneMessage.zone_id == zid).first()

    def effective(col: str | None, default: str) -> str:
        if not col:
            return default
        t = col.strip()
        return t if t else default

    return ResolvedGuestArrivalMessages(
        expected_arrival_message=effective(
            row.expected_arrival_message if row else None,
            DEFAULT_EXPECTED_ARRIVAL_MESSAGE,
        ),
        unexpected_arrival_message=effective(
            row.unexpected_arrival_message if row else None,
            DEFAULT_UNEXPECTED_ARRIVAL_MESSAGE,
        ),
        guest_pass_verified_message=effective(
            row.guest_pass_verified_message if row else None,
            DEFAULT_GUEST_PASS_VERIFIED_MESSAGE,
        ),
    )


def get_guest_arrival_zone_message_row(db: Session, zone_id: str) -> GuestAccessZoneMessage | None:
    zid = (zone_id or "").strip()
    if not zid:
        return None
    return db.query(GuestAccessZoneMessage).filter(GuestAccessZoneMessage.zone_id == zid).first()


def guest_arrival_messages_admin_api_dict(db: Session, zone_id: str) -> dict:
    """Shape for **GET …/guest-arrival-messages** (**defaults** + raw nullable overrides)."""
    zid = (zone_id or "").strip()
    row = get_guest_arrival_zone_message_row(db, zid)
    resolved = resolve_guest_arrival_messages(db, zid)
    return {
        "zone_id": zid,
        "expected_arrival_message": row.expected_arrival_message if row else None,
        "unexpected_arrival_message": row.unexpected_arrival_message if row else None,
        "guest_pass_verified_message": row.guest_pass_verified_message if row else None,
        "defaults": resolved.defaults_dict(),
    }


def upsert_guest_arrival_zone_messages(
    db: Session,
    *,
    zone_id: str,
    column_updates: dict[str, str | None],
    acting_owner_id: int | None,
) -> GuestAccessZoneMessage:
    """Persist only keys present in **column_updates** (SQL column names)."""
    zid = (zone_id or "").strip()
    row = get_guest_arrival_zone_message_row(db, zid)
    if row is None:
        row = GuestAccessZoneMessage(zone_id=zid)
        db.add(row)
        db.flush()
    for k, v in column_updates.items():
        setattr(row, k, v)
    row.updated_by_owner_id = acting_owner_id
    row.updated_at = datetime.utcnow()
    db.flush()
    return row
