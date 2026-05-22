"""Account visibility and ownership rules."""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.services.device_entitlements import (
    assert_account_allows_user_members,
    assert_admin_user_member_capacity,
)


def account_root_id(owner: Owner) -> int:
    """Return the account holder id for an owner."""
    return owner.account_owner_id or owner.id


def resolve_account_owner_id(
    db: Session,
    *,
    role: str,
    requested_account_owner_id: int | None,
    zone_id: str,
    account_type: str,
) -> int | None:
    """Resolve account owner linkage for new owner registrations."""
    if role == "administrator":
        return None
    assert_account_allows_user_members(account_type)

    if requested_account_owner_id is not None:
        account_owner = db.get(Owner, requested_account_owner_id)
        if not account_owner:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account owner not found")
        if str(account_owner.role.value) != "administrator":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="account_owner_id must reference an administrator",
            )
        if str(account_owner.account_type.value) != account_type:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="account_owner_id account type mismatch",
            )
        assert_admin_user_member_capacity(db, account_owner)
        return account_owner.id

    # Fallback to the matching administrator in the same main zone.
    account_owner = (
        db.query(Owner)
        .filter(
            Owner.zone_id == zone_id,
            Owner.account_type == AccountType(account_type),
            Owner.role == OwnerRole.ADMINISTRATOR,
            Owner.active.is_(True),
        )
        .order_by(Owner.id.asc())
        .first()
    )
    if account_owner is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="User registration requires an existing administrator account owner",
        )
    assert_admin_user_member_capacity(db, account_owner)
    return account_owner.id


def visible_owner_ids(db: Session, owner: Owner, include_inactive: bool = False) -> list[int]:
    """Return owners visible to caller based on role/account type rules."""
    # Default-deny for non-admin callers: only explicit administrators can see account-wide owners.
    if owner.role.value != "administrator":
        return [owner.id]

    root_id = account_root_id(owner)
    query = db.query(Owner.id).filter(Owner.account_owner_id == root_id)
    if not include_inactive:
        query = query.filter(Owner.active.is_(True))
    rows = query.all()
    owner_ids = [row[0] for row in rows]
    if owner.id not in owner_ids:
        owner_ids.append(owner.id)
    return owner_ids


def messaging_visible_owner_ids(
    db: Session,
    owner: Owner,
    *,
    include_inactive: bool = False,
    require_same_zone: bool = True,
) -> list[int]:
    """Return owner ids visible for private-message receiver discovery."""
    root_id = account_root_id(owner)
    query = db.query(Owner.id).filter((Owner.id == root_id) | (Owner.account_owner_id == root_id))
    if not include_inactive:
        query = query.filter(Owner.active.is_(True))
    if require_same_zone:
        query = query.filter(Owner.zone_id == owner.zone_id)
    rows = query.all()
    owner_ids = [row[0] for row in rows]
    if owner.id not in owner_ids and (include_inactive or owner.active):
        owner_ids.append(owner.id)
    return owner_ids


def can_message_owner(sender: Owner, receiver: Owner, *, require_same_zone: bool = True) -> bool:
    """Check whether sender can message receiver under account/zone policy."""
    if not receiver.active:
        return False
    if sender.id == receiver.id:
        return False
    if account_root_id(sender) != account_root_id(receiver):
        return False
    if require_same_zone and sender.zone_id != receiver.zone_id:
        return False
    return True


def zone_listing_owner_ids(db: Session, owner: Owner) -> list[int]:
    """Return owner ids whose zones the caller may list or read.

    Administrators see every linked user's zones (same account root).
    Users see only their own zones plus the administrator's main zone (account root).
    """
    if owner.role.value != "administrator":
        root_id = account_root_id(owner)
        if root_id == owner.id:
            return [owner.id]
        return [owner.id, root_id]

    return visible_owner_ids(db, owner)


def visible_zone_owner_ids(db: Session, owner: Owner) -> list[int]:
    """Deprecated alias: use zone_listing_owner_ids."""
    return zone_listing_owner_ids(db, owner)

