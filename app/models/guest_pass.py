"""Guest pass pre-registration model for expected visitor access."""
import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Index, Integer, String, UniqueConstraint

from app.database import Base


class GuestPassStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"


class GuestPass(Base):
    __tablename__ = "guest_passes"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    zone_id = Column(String(100), nullable=False, index=True)
    event_id = Column(String(100), nullable=False, index=True)
    requested_by = Column(Integer, ForeignKey("owners.id", ondelete="SET NULL"), nullable=False, index=True)
    guest_name = Column(String(255), nullable=True)
    notes = Column(String(1000), nullable=True)
    status = Column(
        Enum(GuestPassStatus, name="guestpassstatus", create_constraint=False),
        nullable=False,
        default=GuestPassStatus.PENDING,
        index=True,
    )
    reviewed_by = Column(Integer, ForeignKey("owners.id", ondelete="SET NULL"), nullable=True, index=True)
    used_by_guest_id = Column(String(36), nullable=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("zone_id", "event_id", name="uq_guest_passes_zone_event"),
        Index("ix_guest_passes_zone_event", "zone_id", "event_id"),
        Index("ix_guest_passes_zone_status", "zone_id", "status"),
    )
