"""Centralized per-account zone quota, naming, and edit policy helpers."""
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


def _policy_limits() -> tuple[int, int]:
    max_total = max(1, int(settings.MAX_ZONES_TOTAL))
    reserved = max(0, int(settings.RESERVED_FOR_STANDARD_USERS))
    if reserved >= max_total:
        reserved = max_total - 1
    return max_total, reserved


def lock_account_for_zone_policy(db: Session, root_owner_id: int) -> list[int]:
    """Lock all owners in account scope to avoid quota races."""
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


def count_zones_for_owners(db: Session, owner_ids: Sequence[int]) -> int:
    if not owner_ids:
        return 0
    total = db.execute(select(func.count(Zone.id)).where(Zone.owner_id.in_(tuple(owner_ids)))).scalar()
    return int(total or 0)


def build_capabilities(role: str, total_zones: int) -> ZoneCapabilities:
    max_total, reserved = _policy_limits()
    remaining_total = max(0, max_total - total_zones)
    is_admin = role == "administrator"
    if is_admin:
        remaining_for_role = max(0, max_total - reserved - total_zones)
    else:
        remaining_for_role = remaining_total
    can_create = remaining_for_role > 0 and remaining_total > 0
    reason = None
    if not can_create:
        if remaining_total <= 0:
            reason = "Maximum zone capacity reached for this account."
        elif is_admin:
            reason = "A standard-user slot must remain available."
        else:
            reason = "No standard-user slots remain available."
    return ZoneCapabilities(
        role=role,
        can_create_zone=can_create,
        remaining_total=remaining_total,
        remaining_for_role=remaining_for_role,
        max_total=max_total,
        reserved_for_standard_users=reserved,
        reason=reason,
    )


def enforce_can_create(capabilities: ZoneCapabilities) -> None:
    if capabilities.can_create_zone:
        return
    if capabilities.remaining_total <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "ZONE_QUOTA_MAX_TOTAL_REACHED",
                "message": "Maximum zone capacity has been reached for this account.",
            },
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error_code": "ZONE_QUOTA_RESERVED_FOR_STANDARD",
            "message": "At least one zone slot is reserved for standard users.",
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


def capabilities_for_owner(db: Session, owner: Owner) -> ZoneCapabilities:
    owner_ids = account_owner_ids_for_policy(db, owner)
    total = count_zones_for_owners(db, owner_ids)
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
