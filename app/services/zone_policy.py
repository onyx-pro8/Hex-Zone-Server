"""Zone quota (primary/secondary), naming, visibility, and edit/delete policy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Owner, Zone
from app.services.access_policy import account_root_id
from app.services.account_type_policy import is_system_administrator

ZONE_NAME_MIN_LENGTH = 1
ZONE_NAME_MAX_LENGTH = 120


@dataclass
class EvictedZoneInfo:
    id: int
    name: str
    creator_id: int
    zone_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "creator_id": self.creator_id,
            "zone_id": self.zone_id,
        }


@dataclass
class ZoneCapabilities:
    role: str
    can_create_zone: bool
    remaining_total: int
    remaining_for_role: int
    max_total: int
    reserved_for_standard_users: int
    reason: str | None = None
    admin_primary_count: int = 0
    max_primary: int = 2
    next_zone_is_primary: bool = False
    member_secondary_limit: int = 1
    can_create_primary: bool = False
    can_create_secondary: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "can_create_zone": self.can_create_zone,
            "remaining_total": self.remaining_total,
            "remaining_for_role": self.remaining_for_role,
            "max_total": self.max_total,
            "reserved_for_standard_users": self.reserved_for_standard_users,
            "reason": self.reason,
            "admin_primary_count": self.admin_primary_count,
            "max_primary": self.max_primary,
            "next_zone_is_primary": self.next_zone_is_primary,
            "member_secondary_limit": self.member_secondary_limit,
            "can_create_primary": self.can_create_primary,
            "can_create_secondary": self.can_create_secondary,
        }


def max_admin_zones() -> int:
    """Total zones an administrator may create (primary + secondary)."""
    return max(1, int(settings.MAX_ZONES_ADMINISTRATOR))


def max_admin_primary_zones() -> int:
    """Administrators may mark at most this many of their zones as primary."""
    configured = getattr(settings, "MAX_ZONES_ADMINISTRATOR_PRIMARY", None)
    if configured is not None:
        return max(1, int(configured))
    # Default: up to 2 primary within the admin total (3 → 2 primary + 1 secondary).
    return min(2, max_admin_zones())


def member_secondary_limit_for_primary_count(admin_primary_count: int) -> int:
    """Per-member secondary quota shrinks as the admin claims primary slots.

    With admin total capacity 3: 1 primary → 2 secondary; 2 primary → 1 secondary.
    """
    return max(0, max_admin_zones() - max(0, int(admin_primary_count)))


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
    """Count all zones ever created by this user (active + soft-deleted).

    Create quota is lifetime: deleting a zone does not free a create slot.
    """
    total = db.execute(
        select(func.count(Zone.id)).where(Zone.creator_id == creator_id)
    ).scalar()
    return int(total or 0)


def count_primary_zones_for_creators(db: Session, creator_ids: Sequence[int]) -> int:
    """Count active primary zones (used for member secondary caps / visibility tier)."""
    if not creator_ids:
        return 0
    total = db.execute(
        select(func.count(Zone.id)).where(
            Zone.creator_id.in_(tuple(creator_ids)),
            Zone.is_primary.is_(True),
            Zone.active.is_(True),
        )
    ).scalar()
    return int(total or 0)


def account_admin_owner(db: Session, owner: Owner) -> Owner | None:
    """Return the account administrator for this owner's network account."""
    if (owner.role.value or "").strip().lower() == "administrator":
        return owner
    root_id = account_root_id(owner)
    if root_id == owner.id:
        return owner
    return db.get(Owner, root_id)


def admin_primary_count_for_account(db: Session, owner: Owner) -> int:
    admin = account_admin_owner(db, owner)
    if admin is None:
        return 0
    return count_primary_zones_for_creators(db, [admin.id])


def next_zone_is_primary_for(owner: Owner, admin_primary_count: int) -> bool:
    if (owner.role.value or "").strip().lower() != "administrator":
        return False
    return admin_primary_count < max_admin_primary_zones()


def build_capabilities(
    role: str,
    *,
    total_zones: int,
    admin_primary_count: int,
) -> ZoneCapabilities:
    normalized = (role or "").strip().lower()
    max_primary = max_admin_primary_zones()
    member_limit = member_secondary_limit_for_primary_count(admin_primary_count)

    if normalized == "administrator":
        max_total = max_admin_zones()
        remaining_total = max(0, max_total - total_zones)
        can_primary = remaining_total > 0 and admin_primary_count < max_primary
        can_secondary = remaining_total > 0
        next_is_primary = can_primary
        reason = None
        if remaining_total <= 0:
            reason = (
                f"Maximum of {max_total} zones for administrators reached "
                f"(up to {max_primary} primary). Deleting a zone does not free a create slot."
            )
        return ZoneCapabilities(
            role=role,
            can_create_zone=remaining_total > 0,
            remaining_total=remaining_total,
            remaining_for_role=remaining_total,
            max_total=max_total,
            reserved_for_standard_users=member_limit,
            reason=reason,
            admin_primary_count=admin_primary_count,
            max_primary=max_primary,
            next_zone_is_primary=next_is_primary,
            member_secondary_limit=member_limit,
            can_create_primary=can_primary,
            can_create_secondary=can_secondary,
        )

    max_total = member_limit
    remaining_total = max(0, max_total - total_zones)
    reason = None
    if remaining_total <= 0:
        if max_total <= 0:
            reason = (
                "No secondary zone slots are available while the administrator "
                "holds the maximum primary zones."
            )
        else:
            reason = (
                f"Maximum of {max_total} secondary zone"
                f"{'' if max_total == 1 else 's'} for members reached. "
                "Deleting a zone does not free a create slot."
            )
    return ZoneCapabilities(
        role=role,
        can_create_zone=remaining_total > 0,
        remaining_total=remaining_total,
        remaining_for_role=remaining_total,
        max_total=max_total,
        reserved_for_standard_users=member_limit,
        reason=reason,
        admin_primary_count=admin_primary_count,
        max_primary=max_primary,
        next_zone_is_primary=False,
        member_secondary_limit=member_limit,
        can_create_primary=False,
        can_create_secondary=remaining_total > 0,
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
    """Lock account + creator and evaluate whether this user may create another zone."""
    root_id = account_root_id(owner)
    lock_account_for_zone_policy(db, root_id)
    lock_creator_for_zone_policy(db, owner.id)
    total = count_zones_for_creator(db, owner.id)
    admin_primary = admin_primary_count_for_account(db, owner)
    return build_capabilities(
        owner.role.value,
        total_zones=total,
        admin_primary_count=admin_primary,
    )


def capabilities_for_owner(db: Session, owner: Owner) -> ZoneCapabilities:
    total = count_zones_for_creator(db, owner.id)
    admin_primary = admin_primary_count_for_account(db, owner)
    return build_capabilities(
        owner.role.value,
        total_zones=total,
        admin_primary_count=admin_primary,
    )


def zone_is_primary(zone: Zone) -> bool:
    return bool(getattr(zone, "is_primary", False))


def caller_may_edit_zone(owner: Owner, zone: Zone) -> bool:
    if is_system_administrator(owner):
        return True
    if zone_is_primary(zone):
        return (owner.role.value or "").strip().lower() == "administrator"
    return int(zone.creator_id) == int(owner.id)


def caller_may_delete_zone(owner: Owner, zone: Zone) -> bool:
    return caller_may_edit_zone(owner, zone)


def ensure_zone_edit_allowed(owner: Owner, zone: Zone) -> None:
    if caller_may_edit_zone(owner, zone):
        return
    if zone_is_primary(zone):
        message = "Only the account administrator can edit primary zones."
    else:
        message = "You can edit only secondary zones you created."
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error_code": "ZONE_EDIT_FORBIDDEN", "message": message},
    )


def ensure_zone_delete_allowed(db: Session, owner: Owner, zone: Zone) -> None:
    del db  # kept for call-site compatibility
    if caller_may_delete_zone(owner, zone):
        return
    if zone_is_primary(zone):
        message = "Only the account administrator can delete primary zones."
    else:
        message = "You can delete only secondary zones you created."
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error_code": "ZONE_DELETE_FORBIDDEN", "message": message},
    )


def zone_visible_to_caller(owner: Owner, zone: Zone, *, account_owner_ids: Sequence[int]) -> bool:
    """Primary zones are account-visible; secondary zones are creator-only."""
    if is_system_administrator(owner):
        return True
    if int(zone.owner_id) not in {int(oid) for oid in account_owner_ids}:
        return False
    if zone_is_primary(zone):
        return True
    return int(zone.creator_id) == int(owner.id)


def list_zones_visibility_filter(owner: Owner):
    """SQLAlchemy filter: primary OR creator == caller. System admin: no filter."""
    if is_system_administrator(owner):
        return None
    return or_(Zone.is_primary.is_(True), Zone.creator_id == owner.id)


def soft_delete_zone(zone: Zone) -> None:
    """Mark a zone inactive. Create quota still counts this row."""
    zone.active = False


def evict_member_secondary_overflow(
    db: Session,
    *,
    account_owner_ids: Sequence[int],
    admin_id: int,
    new_primary_count: int,
) -> list[EvictedZoneInfo]:
    """When admin primary count rises, trim each member down to the new secondary max.

    Soft-deletes the member's most recently created active secondary when over quota.
    """
    new_limit = member_secondary_limit_for_primary_count(new_primary_count)
    evicted: list[EvictedZoneInfo] = []
    member_ids = [oid for oid in account_owner_ids if int(oid) != int(admin_id)]
    for member_id in member_ids:
        secondary_rows = (
            db.execute(
                select(Zone)
                .where(
                    Zone.creator_id == member_id,
                    Zone.is_primary.is_(False),
                    Zone.active.is_(True),
                )
                .order_by(Zone.created_at.desc(), Zone.id.desc())
            )
            .scalars()
            .all()
        )
        overflow = max(0, len(secondary_rows) - new_limit)
        for zone in secondary_rows[:overflow]:
            evicted.append(
                EvictedZoneInfo(
                    id=int(zone.id),
                    name=str(zone.name or ""),
                    creator_id=int(zone.creator_id),
                    zone_id=str(zone.zone_id or ""),
                )
            )
            soft_delete_zone(zone)
    if evicted:
        db.flush()
    return evicted


def prepare_zone_tier_on_create(
    db: Session,
    owner: Owner,
    *,
    capabilities: ZoneCapabilities | None = None,
    requested_is_primary: bool | None = None,
) -> tuple[bool, list[EvictedZoneInfo]]:
    """Decide is_primary for the new zone and evict member overflow if needed.

    Administrators may explicitly choose primary vs secondary when both slots
    remain. Members are always secondary. Returns ``(is_primary, evicted_zones)``.
    """
    caps = capabilities or prepare_create_zone_policy(db, owner)
    enforce_can_create(caps)

    is_admin = (owner.role.value or "").strip().lower() == "administrator"
    if not is_admin:
        if requested_is_primary is True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "ZONE_PRIMARY_FORBIDDEN",
                    "message": "Members can create secondary zones only.",
                },
            )
        return False, []

    if requested_is_primary is None:
        will_be_primary = bool(caps.next_zone_is_primary)
    else:
        will_be_primary = bool(requested_is_primary)

    if will_be_primary and not caps.can_create_primary:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "ZONE_PRIMARY_QUOTA_REACHED",
                "message": (
                    f"Maximum of {caps.max_primary} primary zones for "
                    "administrators reached."
                ),
            },
        )
    if not will_be_primary and not caps.can_create_secondary:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "ZONE_QUOTA_MAX_TOTAL_REACHED",
                "message": caps.reason
                or "Maximum zone capacity has been reached for this user.",
            },
        )

    evicted: list[EvictedZoneInfo] = []
    if will_be_primary:
        new_primary_count = caps.admin_primary_count + 1
        root_ids = account_owner_ids_for_policy(db, owner)
        evicted = evict_member_secondary_overflow(
            db,
            account_owner_ids=root_ids,
            admin_id=owner.id,
            new_primary_count=new_primary_count,
        )
    return will_be_primary, evicted
