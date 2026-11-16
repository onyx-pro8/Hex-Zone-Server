"""Subscription plan entitlements — single source of truth for limits.

Network-scoped: one subscription on the account root (network administrator).
Guests have no plan; guest-call minutes consume the host admin's pool.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PlanEntitlements:
    """Limits for one plan / org level."""

    plan_key: str
    account_type: str
    tier_level: int | None
    label: str
    price_monthly: float | None
    price_annual: float | None
    members: int | None  # None = unlimited
    smart_home_hubs: int | None
    messages_per_month: int | None
    history_retention_days: int | None
    ua_keys: int | None
    ua_uses_per_key_per_month: int | None
    access_qr_per_month: int | None
    guest_call_minutes_per_month: int | None
    api_calls_per_month: int | None
    location_updates_per_month: int | None
    allowed_message_types: tuple[str, ...]


# Total monthly message pool (all allowed types combined) — not per-type.
_COMMON_MEMBER_TYPES = ("CHAT", "SERVICE", "PA", "SENSOR", "UNKNOWN", "PANIC", "NS_PANIC", "PERMISSION")
_FAMILY_TYPES = _COMMON_MEMBER_TYPES + ("WELLNESS_CHECK",)
_ORG_TYPES = _FAMILY_TYPES
_INDIVIDUAL_TYPES = ("CHAT", "SERVICE", "PA", "PANIC", "NS_PANIC", "PERMISSION")
_PRO_TYPES = ("CHAT", "SERVICE", "PA", "SENSOR", "UNKNOWN", "PANIC", "NS_PANIC", "PERMISSION")


PLAN_CATALOG: dict[str, PlanEntitlements] = {
    "exclusive": PlanEntitlements(
        plan_key="exclusive",
        account_type="exclusive",
        tier_level=None,
        label="Individual",
        price_monthly=0.0,
        price_annual=0.0,
        members=1,
        smart_home_hubs=0,
        messages_per_month=200,
        history_retention_days=7,
        ua_keys=0,
        ua_uses_per_key_per_month=None,
        access_qr_per_month=0,
        guest_call_minutes_per_month=0,
        api_calls_per_month=None,
        location_updates_per_month=None,
        allowed_message_types=_INDIVIDUAL_TYPES,
    ),
    "enhanced": PlanEntitlements(
        plan_key="enhanced",
        account_type="enhanced",
        tier_level=None,
        label="Individual Pro",
        price_monthly=4.99,
        price_annual=29.88,
        members=1,
        smart_home_hubs=1,
        messages_per_month=1_000,
        history_retention_days=30,
        ua_keys=1,
        ua_uses_per_key_per_month=50,
        access_qr_per_month=10,
        guest_call_minutes_per_month=60,
        api_calls_per_month=25_000,
        location_updates_per_month=10_000,
        allowed_message_types=_PRO_TYPES,
    ),
    "private_plus": PlanEntitlements(
        plan_key="private_plus",
        account_type="private_plus",
        tier_level=None,
        label="Family",
        price_monthly=9.99,
        price_annual=95.88,
        members=10,
        smart_home_hubs=10,
        messages_per_month=5_000,
        history_retention_days=90,
        ua_keys=3,
        ua_uses_per_key_per_month=200,
        access_qr_per_month=50,
        guest_call_minutes_per_month=300,
        api_calls_per_month=100_000,
        location_updates_per_month=50_000,
        allowed_message_types=_FAMILY_TYPES,
    ),
    "enhanced_plus_1": PlanEntitlements(
        plan_key="enhanced_plus_1",
        account_type="enhanced_plus",
        tier_level=1,
        label="Organization 1",
        price_monthly=59.9,
        price_annual=598.8,
        members=20,
        smart_home_hubs=25,
        messages_per_month=25_000,
        history_retention_days=90,
        ua_keys=5,
        ua_uses_per_key_per_month=500,
        access_qr_per_month=100,
        guest_call_minutes_per_month=1_000,
        api_calls_per_month=1_000_000,
        location_updates_per_month=500_000,
        allowed_message_types=_ORG_TYPES,
    ),
    "enhanced_plus_2": PlanEntitlements(
        plan_key="enhanced_plus_2",
        account_type="enhanced_plus",
        tier_level=2,
        label="Organization 2",
        price_monthly=119.0,
        price_annual=1190.0,
        members=50,
        smart_home_hubs=50,
        messages_per_month=50_000,
        history_retention_days=180,
        ua_keys=10,
        ua_uses_per_key_per_month=1_000,
        access_qr_per_month=200,
        guest_call_minutes_per_month=2_000,
        api_calls_per_month=3_000_000,
        location_updates_per_month=500_000,
        allowed_message_types=_ORG_TYPES,
    ),
    "enhanced_plus_3": PlanEntitlements(
        plan_key="enhanced_plus_3",
        account_type="enhanced_plus",
        tier_level=3,
        label="Organization 3",
        price_monthly=219.0,
        price_annual=2268.0,
        members=100,
        smart_home_hubs=100,
        messages_per_month=100_000,
        history_retention_days=180,
        ua_keys=25,
        ua_uses_per_key_per_month=2_500,
        access_qr_per_month=500,
        guest_call_minutes_per_month=5_000,
        api_calls_per_month=3_000_000,
        location_updates_per_month=1_500_000,
        allowed_message_types=_ORG_TYPES,
    ),
    "enhanced_plus_4": PlanEntitlements(
        plan_key="enhanced_plus_4",
        account_type="enhanced_plus",
        tier_level=4,
        label="Organization 4",
        price_monthly=599.0,
        price_annual=5990.0,
        members=500,
        smart_home_hubs=250,
        messages_per_month=250_000,
        history_retention_days=365,
        ua_keys=50,
        ua_uses_per_key_per_month=5_000,
        access_qr_per_month=1_000,
        guest_call_minutes_per_month=10_000,
        api_calls_per_month=7_000_000,
        location_updates_per_month=3_500_000,
        allowed_message_types=_ORG_TYPES,
    ),
    "enhanced_plus_5": PlanEntitlements(
        plan_key="enhanced_plus_5",
        account_type="enhanced_plus",
        tier_level=5,
        label="Organization 5",
        price_monthly=899.0,
        price_annual=10788.0,
        members=2_500,
        smart_home_hubs=500,
        messages_per_month=1_000_000,
        history_retention_days=365,
        ua_keys=100,
        ua_uses_per_key_per_month=15_000,
        access_qr_per_month=5_000,
        guest_call_minutes_per_month=30_000,
        api_calls_per_month=75_000_000,
        location_updates_per_month=45_000_000,
        allowed_message_types=_ORG_TYPES,
    ),
    "private": PlanEntitlements(
        plan_key="private",
        account_type="private",
        tier_level=None,
        label="Private (System)",
        price_monthly=None,
        price_annual=None,
        members=None,
        smart_home_hubs=1,
        messages_per_month=None,
        history_retention_days=None,
        ua_keys=0,
        ua_uses_per_key_per_month=None,
        access_qr_per_month=None,
        guest_call_minutes_per_month=None,
        api_calls_per_month=None,
        location_updates_per_month=None,
        allowed_message_types=_ORG_TYPES,
    ),
}


PAID_CHECKOUT_PLAN_KEYS: tuple[str, ...] = (
    "enhanced",
    "private_plus",
    "enhanced_plus_1",
    "enhanced_plus_2",
    "enhanced_plus_3",
    "enhanced_plus_4",
    "enhanced_plus_5",
)


def plan_key_for(account_type: str, tier_level: int | None = None) -> str:
    key = str(account_type or "").strip().lower()
    if key == "enhanced_plus":
        level = int(tier_level) if tier_level is not None else 1
        if level < 1 or level > 5:
            level = 1
        return f"enhanced_plus_{level}"
    if key in PLAN_CATALOG:
        return key
    return "exclusive"


def entitlements_for(account_type: str, tier_level: int | None = None) -> PlanEntitlements:
    return PLAN_CATALOG[plan_key_for(account_type, tier_level)]


def entitlements_to_dict(plan: PlanEntitlements) -> dict[str, Any]:
    data = asdict(plan)
    data["allowed_message_types"] = list(plan.allowed_message_types)
    return data


def list_public_plans() -> list[dict[str, Any]]:
    """Plans shown on pricing / upgrade UI (excludes Private system)."""
    order = (
        "exclusive",
        "enhanced",
        "private_plus",
        "enhanced_plus_1",
        "enhanced_plus_2",
        "enhanced_plus_3",
        "enhanced_plus_4",
        "enhanced_plus_5",
    )
    return [entitlements_to_dict(PLAN_CATALOG[k]) for k in order]
