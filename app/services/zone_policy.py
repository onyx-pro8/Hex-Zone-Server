"""Centralized per-user zone quota (by creator_id), naming, and edit policy helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Owner, Zone
from app.services.access_policy import account_root_id, visible_zone_owner_ids
from app.services.account_type_policy import is_system_administrator

ZONE_NAME_MIN_LENGTH = 1
ZONE_NAME_MAX_LENGTH = 120


@dataclass
class ZoneCapabilities:
    role: str
    can_create_zone: bool
    remaining_total: int
    remaining_for_role: int
    max_total: int
    reserved_for_standard_users: int
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "can_create_zone": self.can_create_zone,
            "remaining_total": self.remaining_total,
            "remaining_for_role": self.remaining_for_role,
            "max_total": self.max_total,
            "reserved_for_standard_users": self.reserved_for_standard_users,
            "reason": self.reason,
        }


def _max_zones_per_user() -> int:
    return max(1, int(settings.MAX_ZONES_TOTAL))


def lock_account_for_zone_policy(db: Session, root_owner_id: int) -> list[int]:
    """Lock all owners in account scope (used for account-wide name uniqueness)."""
    rows = db.execute(
        select(Owner.id)
        .where((Owner.id == root_owner_id) | (Owner.account_owner_id == root_owner_id))
        .order_by(Owner.id.asc())
        .with_for_update()
    ).all()
    owner_ids = [row[0] for row in rows]
    if root_owner_id not in owner_ids:
        owner_ids.append(root_owner_id)
    return owner_ids


def lock_creator_for_zone_policy(db: Session, creator_id: int) -> int:
    """Lock the creator row to avoid per-user quota races on concurrent creates."""
    row = db.execute(
        select(Owner.id).where(Owner.id == creator_id).with_for_update()
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "OWNER_NOT_FOUND", "message": "Owner not found."},
        )
    return creator_id


def count_zones_for_creator(db: Session, creator_id: int) -> int:
    """Count zones created by this user (`zones.creator_id`)."""
    total = db.execute(
        select(func.count(Zone.id)).where(Zone.creator_id == creator_id)
    ).scalar()
    return int(total or 0)


def build_capabilities(role: str, total_zones: int) -> ZoneCapabilities:
    max_total = _max_zones_per_user()
    remaining_total = max(0, max_total - total_zones)
    can_create = remaining_total > 0
    reason = None
    if not can_create:
        reason = f"Maximum of {max_total} zones per user reached."
    return ZoneCapabilities(
        role=role,
        can_create_zone=can_create,
        remaining_total=remaining_total,
        remaining_for_role=remaining_total,
        max_total=max_total,
        reserved_for_standard_users=0,
        reason=reason,
    )


def enforce_can_create(capabilities: ZoneCapabilities) -> None:
    if capabilities.can_create_zone:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error_code": "ZONE_QUOTA_MAX_TOTAL_REACHED",
            "message": capabilities.reason
            or "Maximum zone capacity has been reached for this user.",
        },
    )


def normalize_zone_name(name: str | None) -> str:
    if name is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "ZONE_NAME_REQUIRED", "message": "Zone name is required."},
        )
    normalized = name.strip()
    if len(normalized) < ZONE_NAME_MIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "ZONE_NAME_REQUIRED", "message": "Zone name is required."},
        )
    if len(normalized) > ZONE_NAME_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "ZONE_NAME_TOO_LONG",
                "message": f"Zone name must be between {ZONE_NAME_MIN_LENGTH} and {ZONE_NAME_MAX_LENGTH} characters.",
            },
        )
    return normalized


def ensure_unique_zone_name(
    db: Session,
    owner_ids: Sequence[int],
    normalized_name: str,
    exclude_zone_record_id: int | None = None,
) -> None:
    if not owner_ids:
        return
    query = select(Zone.id).where(
        Zone.owner_id.in_(tuple(owner_ids)),
        func.lower(Zone.name) == normalized_name.lower(),
    )
    if exclude_zone_record_id is not None:
        query = query.where(Zone.id != exclude_zone_record_id)
    duplicate = db.execute(query).first()
    if duplicate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "ZONE_NAME_DUPLICATE",
                "message": "Zone name must be unique within this account.",
            },
        )


def account_owner_ids_for_policy(db: Session, owner: Owner) -> list[int]:
    root_id = account_root_id(owner)
    return lock_account_for_zone_policy(db, root_id)


def prepare_create_zone_policy(db: Session, owner: Owner) -> ZoneCapabilities:
    """Lock creator and evaluate whether this user may create another zone."""
    lock_creator_for_zone_policy(db, owner.id)
    total = count_zones_for_creator(db, owner.id)
    return build_capabilities(owner.role.value, total)


def capabilities_for_owner(db: Session, owner: Owner) -> ZoneCapabilities:
    total = count_zones_for_creator(db, owner.id)
    return build_capabilities(owner.role.value, total)


def ensure_zone_edit_allowed(owner: Owner, zone: Zone) -> None:
    if is_system_administrator(owner):
        return

    # Option A policy: caller may edit only zones they personally created.
    if zone.creator_id != owner.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "ZONE_EDIT_FORBIDDEN",
                "message": "You can edit only zones you created.",
            },
        )


def ensure_zone_delete_allowed(db: Session, owner: Owner, zone: Zone) -> None:
    """Zone owner may delete; account administrator may delete member zones."""
    if is_system_administrator(owner):
        return

    if zone.owner_id == owner.id:
        return

    if owner.role.value == "administrator":
        allowed_ids = set(visible_zone_owner_ids(db, owner))
        if zone.owner_id in allowed_ids:
            return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error_code": "ZONE_DELETE_FORBIDDEN",
            "message": "You can delete only your own zones, or member zones as the account administrator.",
        },
    )
