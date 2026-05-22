"""Account type constraints for devices and member invitations."""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Owner


DEVICE_LIMITS_BY_ACCOUNT_TYPE: dict[str, int | None] = {
    "private": 1,
    "exclusive": 1,
    "private_plus": 10,
    "enhanced": 1,
    "enhanced_plus": None,
}


# Max number of *user-role* members an administrator may invite under a given
# account type. ``None`` means unlimited; ``0`` means the tier does not support
# user members at all.
USER_MEMBER_LIMITS_BY_ACCOUNT_TYPE: dict[str, int | None] = {
    "private": None,
    "exclusive": 1,
    "private_plus": None,
    "enhanced": None,
    "enhanced_plus": None,
}


def max_devices_for_account_type(account_type: str) -> int | None:
    """Return max devices allowed per owner for an account type."""
    return DEVICE_LIMITS_BY_ACCOUNT_TYPE.get(str(account_type).strip().lower())


def max_user_members_for_account_type(account_type: str) -> int | None:
    """Return max invited user-members allowed for an admin of this tier."""
    return USER_MEMBER_LIMITS_BY_ACCOUNT_TYPE.get(str(account_type).strip().lower())


def assert_owner_device_capacity(owner: Owner, current_device_count: int) -> None:
    """Ensure owner has capacity to enroll another device."""
    max_devices = max_devices_for_account_type(owner.account_type.value)
    if max_devices is None:
        return
    if current_device_count >= max_devices:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Account type '{owner.account_type.value}' allows at most "
                f"{max_devices} device(s) per owner"
            ),
        )


def assert_account_allows_user_members(account_type: str) -> None:
    """Ensure account tier supports user-member registrations at all."""
    limit = max_user_members_for_account_type(account_type)
    if limit == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Account type '{account_type}' does not allow user members",
        )


def _count_active_user_members(db: Session, admin_owner_id: int) -> int:
    """Count active user-role members linked to the given administrator."""
    from app.models.owner import OwnerRole  # local import avoids cycle at module load

    return (
        db.query(Owner.id)
        .filter(
            Owner.account_owner_id == admin_owner_id,
            Owner.role == OwnerRole.USER,
            Owner.active.is_(True),
        )
        .count()
    )


def assert_admin_user_member_capacity(db: Session, admin_owner: Owner) -> None:
    """Ensure the administrator has capacity to add another user member."""
    account_type = admin_owner.account_type.value
    limit = max_user_members_for_account_type(account_type)
    if limit is None:
        return
    if limit == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Account type '{account_type}' does not allow user members",
        )
    current = _count_active_user_members(db, admin_owner.id)
    if current >= limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Account type '{account_type}' allows at most {limit} invited "
                f"user(s) per administrator"
            ),
        )
