"""Contract-oriented zone message history."""
import uuid
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Enum, ForeignKey, Index, String, Text

from app.database import Base
from app.domain.message_types import MessageCategory, MessageScope


class ZoneMessageEvent(Base):
    __tablename__ = "zone_message_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    zone_id = Column(String(100), nullable=False, index=True)
    sender_id = Column(ForeignKey("owners.id", ondelete="SET NULL"), nullable=True, index=True)
    sender_guest_id = Column(String(36), nullable=True, index=True)
    receiver_id = Column(ForeignKey("owners.id", ondelete="SET NULL"), nullable=True, index=True)
    type = Column(String(32), nullable=False, index=True)
    category = Column(Enum(MessageCategory), nullable=False, index=True)
    scope = Column(Enum(MessageScope), nullable=False, index=True)
    text = Column(Text, nullable=False)
    body_json = Column("body", JSON, nullable=False, default=dict)
    metadata_json = Column("metadata", JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_zone_message_events_zone_created", "zone_id", "created_at"),
    )
