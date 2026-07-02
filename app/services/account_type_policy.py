"""Account-type rules for registration and profile updates."""
from __future__ import annotations

from fastapi import HTTPException, status

from app.models import Owner
from app.services.registration_code_service import (
    PRICING_TIER_PRIVATE,
    normalize_pricing_tier_key,
)


PRIVATE_ACCOUNT_PUBLIC_REGISTRATION_DETAIL = (
    "Private accounts are provisioned by the system administrator only."
)


def assert_account_type_allowed_for_public_registration(account_type: str) -> None:
    """Reject self-service registration for the Private (system admin) tier."""
    if normalize_pricing_tier_key(account_type) == PRICING_TIER_PRIVATE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=PRIVATE_ACCOUNT_PUBLIC_REGISTRATION_DETAIL,
        )


def owner_may_edit_network_id(owner: Owner) -> bool:
    """Only Private-tier owners may change their network id."""
    return normalize_pricing_tier_key(owner.account_type.value) == PRICING_TIER_PRIVATE


def assert_owner_may_edit_network_id(owner: Owner) -> None:
    if not owner_may_edit_network_id(owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only system administrator (Private) accounts may change the network ID.",
        )
