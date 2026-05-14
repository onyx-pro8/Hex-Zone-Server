"""Per-zone overrides for guest-facing arrival copy (QR / guest access)."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.database import Base


class GuestAccessZoneMessage(Base):
    """Admin-editable strings keyed by **zone_id** (same string as guest access flows)."""

    __tablename__ = "guest_access_zone_messages"

    id = Column(Integer, primary_key=True, index=True)
    zone_id = Column(String(100), nullable=False, unique=True, index=True)
    expected_arrival_message = Column(String(500), nullable=True)
    unexpected_arrival_message = Column(String(500), nullable=True)
    guest_pass_verified_message = Column(String(500), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    updated_by_owner_id = Column(Integer, ForeignKey("owners.id", ondelete="SET NULL"), nullable=True, index=True)
