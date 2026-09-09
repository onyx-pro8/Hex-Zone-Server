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


def is_system_administrator(owner: Owner) -> bool:
    """True for the built-in Private-tier platform administrator."""
    if owner.role.value != "administrator":
        return False
    return normalize_pricing_tier_key(owner.account_type.value) == PRICING_TIER_PRIVATE


def account_type_for_invited_member(administrator: Owner) -> AccountType:
    """Account type for users invited by a non–system-admin account holder.

    Always Individual (Exclusive). These are *Invited Individuals* linked under
    the inviter via ``account_owner_id`` (Family/Organization member flow).

    System-admin (Private) QR invites are separate: they provision a *Solo*
    Individual on a new network (own account root), not a linked member.
    """
    _ = administrator  # inviter used by callers for linkage / capacity checks
    return AccountType.EXCLUSIVE


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

    - System administrators (Private) may assign any tier to others (Private only
      to administrators) and may change their own tier.
    - Other administrators may change only their own tier, and never to Private.
    """
    new_key = normalize_pricing_tier_key(new_account_type)
    current_key = normalize_pricing_tier_key(target.account_type.value)
    if new_key == current_key:
        return

    if is_system_administrator(caller):
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
        return

    # Non–system-admin: only self-service among non-Private tiers.
    if caller.id != target.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only system administrators may change other users' account types.",
        )
    if caller.role.value != "administrator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators may change account type.",
        )
    if new_key == PRICING_TIER_PRIVATE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Private is reserved for system administrators.",
        )
