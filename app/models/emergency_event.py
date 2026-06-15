"""Dedicated audit log for life-safety alarms (PANIC / NS_PANIC)."""
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text

from app.database import Base


class EmergencyEvent(Base):
    """One row per MAX-priority emergency alarm, kept separate from the message feed.

    This is an immutable forensic record: even if the underlying
    ``zone_message_events`` row is edited or removed, the emergency log retains
    who raised the alarm, where, and how many members it reached.
    """

    __tablename__ = "emergency_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_event_id = Column(
        String(36),
        ForeignKey("zone_message_events.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    type = Column(String(32), nullable=False, index=True)
    sender_id = Column(
        ForeignKey("owners.id", ondelete="SET NULL"), nullable=True, index=True
    )
    zone_id = Column(String(100), nullable=True, index=True)
    recipient_count = Column(Integer, nullable=False, default=0)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_emergency_events_type_created", "type", "created_at"),
    )
