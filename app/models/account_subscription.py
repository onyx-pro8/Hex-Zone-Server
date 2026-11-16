"""Stripe-backed subscription row for a network account root."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, UniqueConstraint

from app.database import Base


class AccountSubscription(Base):
    """Billing state for one network (account-root owner)."""

    __tablename__ = "account_subscriptions"
    __table_args__ = (
        UniqueConstraint("owner_id", name="uq_account_subscriptions_owner_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, nullable=False, index=True)
    stripe_customer_id = Column(String(255), nullable=True, index=True)
    stripe_subscription_id = Column(String(255), nullable=True, index=True)
    stripe_price_id = Column(String(255), nullable=True)
    plan_key = Column(String(64), nullable=False, default="exclusive")
    billing_interval = Column(String(16), nullable=True)  # monthly | annual
    status = Column(String(32), nullable=False, default="none")
    # none | active | trialing | past_due | canceled | unpaid | incomplete
    current_period_end = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class AccountUsageMonthly(Base):
    """Network-scoped monthly usage counters (guest calls, messages, QR, …)."""

    __tablename__ = "account_usage_monthly"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "year_month",
            name="uq_account_usage_monthly_owner_month",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, nullable=False, index=True)
    year_month = Column(String(7), nullable=False)  # YYYY-MM
    messages_sent = Column(Integer, nullable=False, default=0)
    access_qr_minted = Column(Integer, nullable=False, default=0)
    guest_call_seconds = Column(Integer, nullable=False, default=0)
    api_calls = Column(Integer, nullable=False, default=0)
    location_updates = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
