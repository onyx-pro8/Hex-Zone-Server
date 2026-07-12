"""Sender batch replies to recipient wellness asks."""
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text

from app.database import Base


class WellnessSenderReply(Base):
    __tablename__ = "wellness_sender_replies"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_event_id = Column(
        String(36),
        ForeignKey("zone_message_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(String(32), nullable=False, default="ok")
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_wellness_sender_reply_message_created", "message_event_id", "created_at"),
    )
