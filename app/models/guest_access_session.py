"""Ephemeral guest access sessions created by QR arrival requests."""

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

from app.database import Base


class GuestAccessSession(Base):
    """Tracks a guest scan/arrival for permission flow and optional approval."""

    __tablename__ = "guest_access_sessions"

    id = Column(Integer, primary_key=True, index=True)
    guest_id = Column(String(36), nullable=False, unique=True, index=True)
    zone_id = Column(String(100), nullable=False, index=True)
    guest_name = Column(String(255), nullable=False)
    event_id = Column(String(100), nullable=True, index=True)
    device_id = Column(String(255), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    kind = Column(String(32), nullable=False)
    resolution = Column(String(32), nullable=True)
    schedule_id = Column(Integer, ForeignKey("access_schedules.id", ondelete="SET NULL"), nullable=True, index=True)
    admin_owner_id = Column(Integer, ForeignKey("owners.id", ondelete="SET NULL"), nullable=True, index=True)
    qr_token_id = Column(Integer, ForeignKey("guest_access_qr_tokens.id", ondelete="SET NULL"), nullable=True, index=True)
    # One-time guest JWT exchange (minted on admin APPROVED; consumed by POST /api/access/guest-session).
    exchange_code = Column(String(36), nullable=True, unique=True, index=True)
    exchange_expires_at = Column(DateTime, nullable=True)
    exchange_consumed_at = Column(DateTime, nullable=True)
    # Set when admin revokes an active session (expected arrivals) or when a consumed guest pass is revoked.
    access_revoked_at = Column(DateTime, nullable=True, index=True)
    # Snapshot of guest-facing instruction at arrival (expected schedule, guest pass, or pending unexpected).
    arrival_guest_message_snapshot = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
