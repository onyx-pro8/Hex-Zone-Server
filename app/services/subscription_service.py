"""Subscription status, usage meters, and Stripe checkout/portal helpers."""
from __future__ import annotations

import logging
from calendar import monthrange
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.crud import device as device_crud
from app.models.account_subscription import AccountSubscription, AccountUsageMonthly
from app.models.owner import Owner, OwnerRole
from app.models.universal_access import UniversalAccessCredential
from app.services.device_entitlements import (
    is_smart_home_hid,
    max_devices_for_owner,
    max_total_users_for_owner,
)
from app.services.plan_entitlements import (
    PAID_CHECKOUT_PLAN_KEYS,
    entitlements_for,
    entitlements_to_dict,
    list_public_plans,
    plan_key_for,
)

logger = logging.getLogger(__name__)


def resolve_account_root(db: Session, owner: Owner) -> Owner:
    root_id = owner.account_owner_id or owner.id
    if root_id == owner.id:
        return owner
    root = db.query(Owner).filter(Owner.id == root_id).first()
    return root or owner


def caller_may_manage_billing(db: Session, caller: Owner) -> bool:
    """True for the account holder (network admin OR solo Individual).

    Invited Family/Organization members cannot manage billing — only the
    account root. Free Individual (user role) may upgrade their own account.
    """
    root = resolve_account_root(db, caller)
    return int(caller.id) == int(root.id)


def current_year_month(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def month_window(year_month: str) -> tuple[datetime, datetime]:
    year, month = (int(p) for p in year_month.split("-", 1))
    start = datetime(year, month, 1)
    last = monthrange(year, month)[1]
    end = datetime(year, month, last, 23, 59, 59)
    return start, end


def stripe_configured() -> bool:
    return bool((settings.STRIPE_SECRET_KEY or "").strip())


def _get_or_create_usage(db: Session, owner_id: int, year_month: str | None = None) -> AccountUsageMonthly:
    ym = year_month or current_year_month()
    row = (
        db.query(AccountUsageMonthly)
        .filter(
            AccountUsageMonthly.owner_id == owner_id,
            AccountUsageMonthly.year_month == ym,
        )
        .first()
    )
    if row:
        return row
    row = AccountUsageMonthly(owner_id=owner_id, year_month=ym)
    db.add(row)
    db.flush()
    return row


def get_or_create_subscription_row(db: Session, root: Owner) -> AccountSubscription:
    row = (
        db.query(AccountSubscription)
        .filter(AccountSubscription.owner_id == root.id)
        .first()
    )
    if row:
        return row
    plan_key = plan_key_for(root.account_type.value, getattr(root, "tier_level", None))
    row = AccountSubscription(
        owner_id=root.id,
        plan_key=plan_key,
        status="active" if plan_key == "exclusive" else "none",
    )
    db.add(row)
    db.flush()
    return row


def _count_members(db: Session, root: Owner) -> int:
    admin_active = 1 if bool(root.active) else 0
    members = (
        db.query(Owner.id)
        .filter(
            Owner.account_owner_id == root.id,
            Owner.id != root.id,
            Owner.role == OwnerRole.USER,
            Owner.active.is_(True),
        )
        .count()
    )
    return admin_active + members


def _count_smart_home_hubs(db: Session, root: Owner) -> int:
    devices = device_crud.list_devices(db, owner_id=root.id)
    return sum(1 for d in devices if is_smart_home_hid(getattr(d, "hid", None)))


def _count_ua_keys(db: Session, root: Owner) -> int:
    return (
        db.query(UniversalAccessCredential.id)
        .filter(
            UniversalAccessCredential.network_admin_id == root.id,
            UniversalAccessCredential.enabled.is_(True),
        )
        .count()
    )


def _count_messages_sent(db: Session, root: Owner, year_month: str) -> int:
    """Prefer metered counter; fall back to Message rows from network members this month."""
    usage = (
        db.query(AccountUsageMonthly)
        .filter(
            AccountUsageMonthly.owner_id == root.id,
            AccountUsageMonthly.year_month == year_month,
        )
        .first()
    )
    if usage and int(usage.messages_sent or 0) > 0:
        return int(usage.messages_sent)

    from app.models.message import Message

    start, end = month_window(year_month)
    member_ids = [
        row.id
        for row in db.query(Owner.id)
        .filter(
            (Owner.id == root.id) | (Owner.account_owner_id == root.id),
            Owner.active.is_(True),
        )
        .all()
    ]
    if not member_ids:
        member_ids = [root.id]
    return (
        db.query(Message.id)
        .filter(
            Message.sender_id.in_(member_ids),
            Message.created_at >= start,
            Message.created_at <= end,
        )
        .count()
    )


def _meter_value(usage: AccountUsageMonthly | None, field: str) -> int:
    if not usage:
        return 0
    return int(getattr(usage, field, 0) or 0)


def _feature_row(
    *,
    key: str,
    label: str,
    used: int | float,
    limit: int | None,
    unit: str = "count",
) -> dict[str, Any]:
    unlimited = limit is None
    remaining: float | None
    if unlimited:
        remaining = None
        pct = 0.0
    else:
        lim = float(limit)
        remaining = max(0.0, lim - float(used))
        pct = 0.0 if lim <= 0 else min(100.0, round(100.0 * float(used) / lim, 1))
    return {
        "key": key,
        "label": label,
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "percent_used": pct,
        "unlimited": unlimited,
        "unit": unit,
    }


def build_subscription_overview(db: Session, caller: Owner) -> dict[str, Any]:
    root = resolve_account_root(db, caller)
    plan = entitlements_for(root.account_type.value, getattr(root, "tier_level", None))
    sub = get_or_create_subscription_row(db, root)
    ym = current_year_month()
    usage = (
        db.query(AccountUsageMonthly)
        .filter(
            AccountUsageMonthly.owner_id == root.id,
            AccountUsageMonthly.year_month == ym,
        )
        .first()
    )

    members_used = _count_members(db, root)
    hubs_used = _count_smart_home_hubs(db, root)
    ua_used = _count_ua_keys(db, root)
    messages_used = _count_messages_sent(db, root, ym)
    qr_used = _meter_value(usage, "access_qr_minted")
    call_seconds = _meter_value(usage, "guest_call_seconds")
    call_minutes_used = round(call_seconds / 60.0, 1)
    api_used = _meter_value(usage, "api_calls")

    # Prefer catalog; fall back to live device entitlement helpers when present.
    hub_limit = plan.smart_home_hubs
    live_hub = max_devices_for_owner(root)
    if live_hub is not None:
        hub_limit = live_hub
    member_limit = plan.members
    live_members = max_total_users_for_owner(root)
    if live_members is not None:
        member_limit = live_members

    features = [
        _feature_row(key="members", label="Members", used=members_used, limit=member_limit),
        _feature_row(
            key="smart_home_hubs",
            label="Smart-home hubs",
            used=hubs_used,
            limit=hub_limit,
        ),
        _feature_row(
            key="messages",
            label="Messages (this month)",
            used=messages_used,
            limit=plan.messages_per_month,
        ),
        _feature_row(
            key="history_retention_days",
            label="Message & access history",
            used=plan.history_retention_days or 0,
            limit=plan.history_retention_days,
            unit="days",
        ),
        _feature_row(
            key="ua_keys",
            label="Universal Access keys",
            used=ua_used,
            limit=plan.ua_keys,
        ),
        _feature_row(
            key="access_qr",
            label="Access QR codes (this month)",
            used=qr_used,
            limit=plan.access_qr_per_month,
        ),
        _feature_row(
            key="guest_call_minutes",
            label="Guest call minutes (this month)",
            used=call_minutes_used,
            limit=plan.guest_call_minutes_per_month,
            unit="minutes",
        ),
        _feature_row(
            key="api_calls",
            label="API calls (this month)",
            used=api_used,
            limit=plan.api_calls_per_month,
        ),
    ]

    manages_billing = caller_may_manage_billing(db, caller)
    is_individual = plan.plan_key == "exclusive"
    is_member = int(caller.id) != int(root.id)

    db.commit()

    if is_member:
        scope_note = (
            "This network plan is managed by the account holder. "
            "Limits are shared across the whole network."
        )
    elif is_individual:
        scope_note = (
            "Individual is free (user role). Upgrade to Individual Pro, Family, "
            "or Organization — after payment your account type and role update automatically. "
            "Guests have no subscription."
        )
    else:
        scope_note = (
            "Limits are shared across the whole network account. "
            "Guest call minutes belong to this plan — guests have no subscription. "
            "Account type changes automatically after a successful upgrade."
        )

    return {
        "network_owner_id": root.id,
        "viewer_owner_id": caller.id,
        "viewer_is_network_admin": manages_billing and not is_individual,
        "viewer_is_member": is_member,
        "plan": entitlements_to_dict(plan),
        "subscription": {
            "plan_key": sub.plan_key or plan.plan_key,
            "status": sub.status,
            "billing_interval": sub.billing_interval,
            "stripe_customer_id": sub.stripe_customer_id,
            "stripe_subscription_id": sub.stripe_subscription_id,
            "current_period_end": (
                sub.current_period_end.isoformat() + "Z" if sub.current_period_end else None
            ),
            "cancel_at_period_end": bool(sub.cancel_at_period_end),
        },
        "stripe_configured": stripe_configured(),
        "year_month": ym,
        "features": features,
        "can_manage_billing": manages_billing,
        "upgrade_plans": list_public_plans(),
        "scope_note": scope_note,
    }


def record_guest_call_seconds(
    db: Session,
    *,
    admin_owner_id: int,
    seconds: int,
) -> AccountUsageMonthly:
    """Add connected guest-call duration to the host network pool."""
    secs = max(0, int(seconds))
    usage = _get_or_create_usage(db, admin_owner_id)
    if secs > 0:
        usage.guest_call_seconds = int(usage.guest_call_seconds or 0) + secs
    db.commit()
    return usage


def guest_call_minutes_remaining(db: Session, admin_owner: Owner) -> float | None:
    """None = unlimited; otherwise remaining minutes this month."""
    plan = entitlements_for(
        admin_owner.account_type.value,
        getattr(admin_owner, "tier_level", None),
    )
    if plan.guest_call_minutes_per_month is None:
        return None
    usage = (
        db.query(AccountUsageMonthly)
        .filter(
            AccountUsageMonthly.owner_id == admin_owner.id,
            AccountUsageMonthly.year_month == current_year_month(),
        )
        .first()
    )
    used = (_meter_value(usage, "guest_call_seconds") / 60.0) if usage else 0.0
    return max(0.0, float(plan.guest_call_minutes_per_month) - used)


def assert_guest_call_allowed(db: Session, admin_owner: Owner) -> None:
    remaining = guest_call_minutes_remaining(db, admin_owner)
    if remaining is None:
        return
    if remaining <= 0:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                "Guest call minutes for this network are exhausted this month. "
                "Upgrade the network plan in User settings → Subscription."
            ),
        )


def record_access_qr_mint(db: Session, admin_owner_id: int, count: int = 1) -> None:
    """Bump QR mint counter; does not commit (caller owns the transaction)."""
    usage = _get_or_create_usage(db, admin_owner_id)
    usage.access_qr_minted = int(usage.access_qr_minted or 0) + max(1, int(count))
    db.flush()


def record_message_send(db: Session, root_owner_id: int, count: int = 1) -> None:
    """Bump message counter; does not commit (caller owns the transaction)."""
    usage = _get_or_create_usage(db, root_owner_id)
    usage.messages_sent = int(usage.messages_sent or 0) + max(1, int(count))
    db.flush()


def _stripe_client():
    if not stripe_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe is not configured on this server. Set STRIPE_SECRET_KEY to enable billing.",
        )
    try:
        import stripe
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe SDK is not installed on the server.",
        ) from exc
    stripe.api_key = settings.STRIPE_SECRET_KEY.strip()
    return stripe


def _price_id_for_plan(plan_key: str, billing_interval: str) -> str:
    interval = "annual" if billing_interval == "annual" else "monthly"
    mapping = {
        ("enhanced", "monthly"): settings.STRIPE_PRICE_ENHANCED_MONTHLY,
        ("enhanced", "annual"): settings.STRIPE_PRICE_ENHANCED_ANNUAL,
        ("private_plus", "monthly"): settings.STRIPE_PRICE_PRIVATE_PLUS_MONTHLY,
        ("private_plus", "annual"): settings.STRIPE_PRICE_PRIVATE_PLUS_ANNUAL,
        ("enhanced_plus_1", "monthly"): settings.STRIPE_PRICE_ORG_1_MONTHLY,
        ("enhanced_plus_1", "annual"): settings.STRIPE_PRICE_ORG_1_ANNUAL,
        ("enhanced_plus_2", "monthly"): settings.STRIPE_PRICE_ORG_2_MONTHLY,
        ("enhanced_plus_2", "annual"): settings.STRIPE_PRICE_ORG_2_ANNUAL,
        ("enhanced_plus_3", "monthly"): settings.STRIPE_PRICE_ORG_3_MONTHLY,
        ("enhanced_plus_3", "annual"): settings.STRIPE_PRICE_ORG_3_ANNUAL,
        ("enhanced_plus_4", "monthly"): settings.STRIPE_PRICE_ORG_4_MONTHLY,
        ("enhanced_plus_4", "annual"): settings.STRIPE_PRICE_ORG_4_ANNUAL,
        ("enhanced_plus_5", "monthly"): settings.STRIPE_PRICE_ORG_5_MONTHLY,
        ("enhanced_plus_5", "annual"): settings.STRIPE_PRICE_ORG_5_ANNUAL,
    }
    price_id = (mapping.get((plan_key, interval)) or "").strip()
    if not price_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Stripe price is not configured for plan '{plan_key}' ({interval}).",
        )
    return price_id


def create_checkout_session(
    db: Session,
    *,
    caller: Owner,
    plan_key: str,
    billing_interval: str = "monthly",
) -> dict[str, Any]:
    if not caller_may_manage_billing(db, caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only the account holder can manage billing. "
                "Invited members cannot upgrade the network plan."
            ),
        )
    root = resolve_account_root(db, caller)
    key = str(plan_key or "").strip().lower()
    if key not in PAID_CHECKOUT_PLAN_KEYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Select a paid plan to checkout.",
        )
    interval = "annual" if str(billing_interval).lower() == "annual" else "monthly"
    stripe = _stripe_client()
    sub = get_or_create_subscription_row(db, root)

    if not sub.stripe_customer_id:
        customer = stripe.Customer.create(
            email=root.email,
            name=f"{root.first_name} {root.last_name}".strip() or root.email,
            metadata={"owner_id": str(root.id), "zone_id": root.zone_id or ""},
        )
        sub.stripe_customer_id = customer["id"]
        db.commit()

    price_id = _price_id_for_plan(key, interval)
    success = (settings.STRIPE_SUCCESS_URL or settings.MOBILE_SUBSCRIPTION_RETURN_URL or "").strip()
    cancel = (settings.STRIPE_CANCEL_URL or settings.MOBILE_SUBSCRIPTION_RETURN_URL or "").strip()
    if not success:
        success = (
            "safezonepatrol://subscription?checkout=success&session_id={CHECKOUT_SESSION_ID}"
        )
    if "{CHECKOUT_SESSION_ID}" not in success:
        joiner = "&" if "?" in success else "?"
        success = f"{success}{joiner}session_id={{CHECKOUT_SESSION_ID}}"
    if not cancel:
        cancel = "safezonepatrol://subscription?checkout=cancel"

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=sub.stripe_customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success,
        cancel_url=cancel,
        client_reference_id=str(root.id),
        metadata={
            "owner_id": str(root.id),
            "plan_key": key,
            "billing_interval": interval,
        },
        subscription_data={
            "metadata": {
                "owner_id": str(root.id),
                "plan_key": key,
                "billing_interval": interval,
            }
        },
    )
    db.commit()
    return {
        "checkout_url": session["url"],
        "session_id": session["id"],
        "plan_key": key,
        "billing_interval": interval,
    }


def create_billing_portal_session(db: Session, *, caller: Owner) -> dict[str, Any]:
    if not caller_may_manage_billing(db, caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only the account holder can manage billing. "
                "Invited members cannot open the billing portal."
            ),
        )
    root = resolve_account_root(db, caller)
    stripe = _stripe_client()
    sub = get_or_create_subscription_row(db, root)
    if not sub.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Stripe customer yet. Start a checkout first.",
        )
    return_url = (
        (settings.STRIPE_PORTAL_RETURN_URL or settings.MOBILE_SUBSCRIPTION_RETURN_URL or "").strip()
        or "safezonepatrol://subscription"
    )
    portal = stripe.billing_portal.Session.create(
        customer=sub.stripe_customer_id,
        return_url=return_url,
    )
    return {"portal_url": portal["url"]}


def _apply_plan_to_owner(db: Session, root: Owner, plan_key: str) -> None:
    """Apply paid plan entitlements and lock role to the plan rules.

    Individual (exclusive) stays user-role. Individual Pro / Family /
    Organization become administrator. No manual account-type picker needed.
    """
    from app.models.owner import AccountType, OwnerRole
    from app.services.plan_entitlements import PLAN_CATALOG

    entitlements = PLAN_CATALOG.get(plan_key)
    if entitlements is None:
        entitlements = entitlements_for(plan_key)
    new_type = entitlements.account_type
    root.account_type = AccountType(new_type)
    root.tier_level = entitlements.tier_level
    root.expired = False
    if new_type == "exclusive":
        root.role = OwnerRole.USER
    else:
        # enhanced / private_plus / enhanced_plus / private
        root.role = OwnerRole.ADMINISTRATOR
    # Solo upgraded accounts are their own network root.
    if root.account_owner_id is None or int(root.account_owner_id) == int(root.id):
        root.account_owner_id = root.id
    db.add(root)


def _session_get(session_obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(session_obj, dict):
        return session_obj.get(key, default)
    try:
        value = session_obj[key]
    except Exception:
        value = getattr(session_obj, key, default)
    return default if value is None else value


def apply_checkout_completed(db: Session, session_obj: Any) -> Owner | None:
    """Persist subscription + upgrade owner account_type/role. Returns owner or None."""
    metadata = _session_get(session_obj, "metadata") or {}
    if not isinstance(metadata, dict):
        try:
            metadata = dict(metadata)
        except Exception:
            metadata = {}
    owner_raw = metadata.get("owner_id") or _session_get(session_obj, "client_reference_id") or 0
    try:
        owner_id = int(owner_raw)
    except (TypeError, ValueError):
        owner_id = 0
    plan_key = str(metadata.get("plan_key") or "").strip().lower()
    interval = str(metadata.get("billing_interval") or "monthly").strip().lower()
    if not owner_id or plan_key not in PAID_CHECKOUT_PLAN_KEYS:
        logger.warning("Stripe checkout missing owner/plan metadata: %s", metadata)
        return None
    root = db.query(Owner).filter(Owner.id == owner_id).first()
    if not root:
        return None
    sub = get_or_create_subscription_row(db, root)
    customer = _session_get(session_obj, "customer")
    subscription_id = _session_get(session_obj, "subscription")
    if customer:
        sub.stripe_customer_id = str(customer)
    if subscription_id:
        sub.stripe_subscription_id = str(subscription_id)
    sub.plan_key = plan_key
    sub.billing_interval = interval
    sub.status = "active"
    _apply_plan_to_owner(db, root, plan_key)
    db.commit()
    db.refresh(root)
    logger.info(
        "Applied Stripe checkout for owner=%s plan_key=%s account_type=%s role=%s",
        root.id,
        plan_key,
        root.account_type.value,
        root.role.value,
    )
    return root


def confirm_checkout_session(
    db: Session,
    *,
    caller: Owner,
    session_id: str,
) -> dict[str, Any]:
    """Confirm Checkout after mobile deep-link return (works without webhook)."""
    if not caller_may_manage_billing(db, caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the account holder can confirm checkout.",
        )
    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="session_id is required.",
        )
    stripe = _stripe_client()
    try:
        session_obj = stripe.checkout.Session.retrieve(sid)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not load Checkout session: {exc}",
        ) from exc

    metadata = _session_get(session_obj, "metadata") or {}
    if not isinstance(metadata, dict):
        try:
            metadata = dict(metadata)
        except Exception:
            metadata = {}
    try:
        meta_owner_id = int(metadata.get("owner_id") or _session_get(session_obj, "client_reference_id") or 0)
    except (TypeError, ValueError):
        meta_owner_id = 0
    root = resolve_account_root(db, caller)
    if meta_owner_id and meta_owner_id != int(root.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This Checkout session belongs to a different account.",
        )

    status_value = str(_session_get(session_obj, "status") or "")
    payment_status = str(_session_get(session_obj, "payment_status") or "")
    paid = status_value == "complete" and payment_status in {
        "paid",
        "no_payment_required",
    }
    if not paid:
        return {
            "ok": False,
            "status": status_value or "open",
            "payment_status": payment_status or "unpaid",
            "message": "Payment was not completed.",
            "plan": entitlements_to_dict(
                entitlements_for(root.account_type.value, getattr(root, "tier_level", None))
            ),
            "account_type": root.account_type.value,
            "role": root.role.value,
            "tier_level": getattr(root, "tier_level", None),
        }

    updated = apply_checkout_completed(db, session_obj)
    owner = updated or root
    db.refresh(owner)
    plan = entitlements_for(owner.account_type.value, getattr(owner, "tier_level", None))
    return {
        "ok": True,
        "status": "complete",
        "payment_status": payment_status,
        "message": "Subscription activated.",
        "plan": entitlements_to_dict(plan),
        "account_type": owner.account_type.value,
        "role": owner.role.value,
        "tier_level": getattr(owner, "tier_level", None),
    }


def apply_subscription_updated(db: Session, subscription_obj: dict[str, Any]) -> None:
    metadata = subscription_obj.get("metadata") or {}
    owner_id_raw = metadata.get("owner_id")
    sub_id = subscription_obj.get("id")
    row = None
    if owner_id_raw:
        row = (
            db.query(AccountSubscription)
            .filter(AccountSubscription.owner_id == int(owner_id_raw))
            .first()
        )
    if row is None and sub_id:
        row = (
            db.query(AccountSubscription)
            .filter(AccountSubscription.stripe_subscription_id == sub_id)
            .first()
        )
    if row is None:
        return
    root = db.query(Owner).filter(Owner.id == row.owner_id).first()
    status_value = str(subscription_obj.get("status") or "active")
    row.status = status_value
    row.stripe_subscription_id = sub_id or row.stripe_subscription_id
    row.cancel_at_period_end = bool(subscription_obj.get("cancel_at_period_end"))
    period_end = subscription_obj.get("current_period_end")
    if period_end:
        row.current_period_end = datetime.utcfromtimestamp(int(period_end))
    plan_key = str(metadata.get("plan_key") or row.plan_key or "").strip().lower()
    if plan_key in PAID_CHECKOUT_PLAN_KEYS:
        row.plan_key = plan_key
        if root and status_value in {"active", "trialing"}:
            _apply_plan_to_owner(db, root, plan_key)
    if root and status_value in {"canceled", "unpaid", "incomplete_expired"}:
        root.expired = True
        db.add(root)
    db.commit()


def handle_stripe_webhook(db: Session, payload: bytes, signature: str | None) -> dict[str, str]:
    stripe = _stripe_client()
    secret = (settings.STRIPE_WEBHOOK_SECRET or "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="STRIPE_WEBHOOK_SECRET is not configured.",
        )
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid Stripe webhook: {exc}",
        ) from exc

    event_type = event["type"]
    data_object = event["data"]["object"]
    if event_type == "checkout.session.completed":
        apply_checkout_completed(db, data_object)
    elif event_type in {
        "customer.subscription.updated",
        "customer.subscription.created",
        "customer.subscription.deleted",
    }:
        apply_subscription_updated(db, data_object)
    return {"received": "ok", "type": event_type}
