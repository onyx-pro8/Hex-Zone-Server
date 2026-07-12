"""Recipient requests for the wellness-check sender to confirm they are OK."""
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, String

from app.database import Base


class WellnessRecipientAsk(Base):
    __tablename__ = "wellness_recipient_asks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_event_id = Column(
        String(36),
        ForeignKey("zone_message_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asker_owner_id = Column(
        ForeignKey("owners.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sender_reply_id = Column(
        String(36),
        ForeignKey("wellness_sender_replies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_wellness_recipient_ask_message_created", "message_event_id", "created_at"),
        Index(
            "ix_wellness_recipient_ask_pending",
            "message_event_id",
            "asker_owner_id",
            "sender_reply_id",
        ),
    )
