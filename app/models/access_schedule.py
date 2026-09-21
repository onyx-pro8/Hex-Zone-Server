"""Access schedule records used by permission flow."""
import enum
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Index, Integer, String

from app.database import Base


class AccessScheduleStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"


class AccessSchedule(Base):
    __tablename__ = "access_schedules"

    id = Column(Integer, primary_key=True, index=True)
    zone_id = Column(String(100), nullable=False, index=True)
    event_id = Column(String(100), nullable=True, index=True)
    guest_id = Column(String(100), nullable=True, index=True)
    guest_name = Column(String(255), nullable=True, index=True)
    starts_at = Column(DateTime, nullable=True, index=True)
    ends_at = Column(DateTime, nullable=True, index=True)
    notify_member_assist = Column(Boolean, default=False, nullable=False)
    # True only while status == ACCEPTED (denormalized for arrival matching).
    active = Column(Boolean, default=False, nullable=False, index=True)
    status = Column(
        Enum(AccessScheduleStatus, name="accessschedulestatus", create_constraint=False),
        nullable=False,
        default=AccessScheduleStatus.PENDING,
        index=True,
    )
    created_by_owner_id = Column(
        Integer, ForeignKey("owners.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reviewed_by = Column(
        Integer, ForeignKey("owners.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_access_schedule_zone_event_guest", "zone_id", "event_id", "guest_id"),
        Index("ix_access_schedules_zone_status", "zone_id", "status"),
    )
