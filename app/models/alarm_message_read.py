"""Per-user read receipts for alarm-category zone message events."""
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, UniqueConstraint

from app.database import Base


class AlarmMessageRead(Base):
    __tablename__ = "alarm_message_reads"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_event_id = Column(
        String(36),
        ForeignKey("zone_message_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_id = Column(
        ForeignKey("owners.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint(
            "message_event_id",
            "owner_id",
            name="uq_alarm_read_message_owner",
        ),
        Index("ix_alarm_read_message_created", "message_event_id", "created_at"),
    )
