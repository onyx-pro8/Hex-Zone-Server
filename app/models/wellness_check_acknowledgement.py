"""Acknowledgements for WELLNESS_CHECK zone message events."""
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, UniqueConstraint

from app.database import Base


class WellnessCheckAcknowledgement(Base):
    __tablename__ = "wellness_check_acknowledgements"

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
    status = Column(String(32), nullable=False, default="ok")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint(
            "message_event_id",
            "owner_id",
            name="uq_wellness_ack_message_owner",
        ),
        Index("ix_wellness_ack_message_created", "message_event_id", "created_at"),
    )
