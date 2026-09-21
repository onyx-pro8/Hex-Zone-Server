"""Account-type rules for registration and profile updates."""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.services.registration_code_service import (
    PRICING_TIER_PRIVATE,
    normalize_pricing_tier_key,
)


PRIVATE_ACCOUNT_PUBLIC_REGISTRATION_DETAIL = (
    "Private accounts are provisioned by the system administrator only."
)

INDIVIDUAL_ACCOUNT_USER_ROLE_ONLY_DETAIL = (
    "Individual accounts always use the user role and cannot be administrators."
)

ROLE_CHANGE_SYSTEM_ADMIN_ONLY_DETAIL = (
    "Only system administrators may change user roles."
)

ACCOUNT_TYPE_CHANGE_SYSTEM_ADMIN_ONLY_DETAIL = (
    "Only system administrators may change account types."
)


def is_individual_account_type(account_type: str | AccountType | None) -> bool:
    """True for Exclusive / Individual tier."""
    if account_type is None:
        return False
    value = account_type.value if isinstance(account_type, AccountType) else str(account_type)
    return normalize_pricing_tier_key(value) == "exclusive"


def assert_account_type_allowed_for_public_registration(account_type: str) -> None:
    """Reject self-service registration for the Private (system admin) tier."""
    if normalize_pricing_tier_key(account_type) == PRICING_TIER_PRIVATE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=PRIVATE_ACCOUNT_PUBLIC_REGISTRATION_DETAIL,
        )


def coerce_individual_registration_role(account_type: str, role: OwnerRole) -> OwnerRole:
    """Individual accounts always register as user (never administrator)."""
    if is_individual_account_type(account_type):
        return OwnerRole.USER
    return role


def assert_individual_role_change_allowed(
    *,
    account_type: str | AccountType | None,
    new_role: str | OwnerRole | None,
) -> None:
    """Reject promoting an Individual account to administrator."""
    if new_role is None or not is_individual_account_type(account_type):
        return
    role_value = new_role.value if isinstance(new_role, OwnerRole) else str(new_role)
    if role_value.strip().lower() == "administrator":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=INDIVIDUAL_ACCOUNT_USER_ROLE_ONLY_DETAIL,
        )


def assert_role_change_allowed(
    *,
    caller: Owner,
    account_type: str | AccountType | None = None,
    new_role: str | OwnerRole | None = None,
) -> None:
    """Only system administrators may change roles; Individuals stay user-only."""
    if new_role is None:
        return
    if not is_system_administrator(caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ROLE_CHANGE_SYSTEM_ADMIN_ONLY_DETAIL,
        )
    assert_individual_role_change_allowed(
        account_type=account_type,
        new_role=new_role,
    )


def is_system_administrator(owner: Owner) -> bool:
    """True for the built-in Private-tier platform administrator."""
    if owner.role.value != "administrator":
        return False
    return normalize_pricing_tier_key(owner.account_type.value) == PRICING_TIER_PRIVATE


def account_type_for_invited_member(administrator: Owner) -> AccountType:
    """Account type for users invited by a non–system-admin account holder.

    - Family (private_plus) / Organization (enhanced_plus): same type as inviter,
      role stays ``user``.
    - Individual Pro (enhanced): invited seat is Individual (exclusive).
    - Other tiers: Individual (exclusive).

    System-admin (Private) QR invites are separate: they provision a *Solo*
    Individual on a new network (own account root), not a linked member.
    """
    key = normalize_pricing_tier_key(administrator.account_type.value)
    if key in {"private_plus", "enhanced_plus"}:
        return administrator.account_type
    return AccountType.EXCLUSIVE


def migrate_invited_member_account_types(db: Session) -> int:
    """Align linked Family/Organization members with their account holder's type.

    Legacy rows stored invited members as Exclusive; Family/Org members should
    share the administrator's ``private_plus`` / ``enhanced_plus`` type (role
    remains user). Individual Pro invitees stay Exclusive and are left alone.
    Returns the number of owners updated.
    """
    updated = 0
    admins = (
        db.query(Owner)
        .filter(
            Owner.role == OwnerRole.ADMINISTRATOR,
            Owner.account_type.in_((AccountType.PRIVATE_PLUS, AccountType.ENHANCED_PLUS)),
            Owner.active.is_(True),
        )
        .all()
    )
    for admin in admins:
        root_id = admin.account_owner_id or admin.id
        member_type = account_type_for_invited_member(admin)
        members = (
            db.query(Owner)
            .filter(
                Owner.account_owner_id == root_id,
                Owner.id != admin.id,
                Owner.account_type != member_type,
            )
            .all()
        )
        for member in members:
            member.account_type = member_type
            updated += 1
    if updated:
        db.commit()
    return updated


def owner_may_edit_network_id(owner: Owner) -> bool:
    """Only Private-tier owners may change their network id (zone_id)."""
    return is_system_administrator(owner)


def assert_owner_may_edit_network_id(owner: Owner) -> None:
    if not owner_may_edit_network_id(owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only system administrator (Private) accounts may change the network ID.",
        )


def assert_system_administrator_may_set_account_type(caller: Owner) -> None:
    """Only Private-tier administrators may assign account types."""
    if not is_system_administrator(caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only system administrators may change account types.",
        )


def count_system_administrators(db: Session) -> int:
    """Count active administrator accounts on the Private (system) tier."""
    return (
        db.query(Owner)
        .filter(
            Owner.role == OwnerRole.ADMINISTRATOR,
            Owner.account_type == AccountType.PRIVATE,
            Owner.active.is_(True),
        )
        .count()
    )


def assert_account_type_change_allowed(
    db: Session,
    caller: Owner,
    target: Owner,
    new_account_type: str,
) -> None:
    """Validate an account-type assignment.

    Only system administrators (Private) may change account types — for themselves
    or any other user. Private may only be assigned to administrator accounts.
    """
    new_key = normalize_pricing_tier_key(new_account_type)
    current_key = normalize_pricing_tier_key(target.account_type.value)
    if new_key == current_key:
        return

    if not is_system_administrator(caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ACCOUNT_TYPE_CHANGE_SYSTEM_ADMIN_ONLY_DETAIL,
        )

    if (
        is_system_administrator(target)
        and new_key != PRICING_TIER_PRIVATE
        and count_system_administrators(db) <= 1
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot change account type: at least one system administrator is required.",
        )
    if new_key == PRICING_TIER_PRIVATE and target.role.value != "administrator":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only administrator accounts may be assigned the Private (system) account type.",
        )
