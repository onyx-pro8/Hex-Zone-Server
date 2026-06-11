"""Zone message model."""
from datetime import datetime
import enum
from sqlalchemy import Boolean, Column, Integer, Text, DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.orm import relationship
from app.database import Base
from app.domain.message_types import CanonicalMessageType, MessageScope


class MessageVisibility(str, enum.Enum):
    """Message visibility enumeration."""

    PUBLIC = MessageScope.PUBLIC.value
    PRIVATE = MessageScope.PRIVATE.value


class Message(Base):
    """Zone message model."""

    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    receiver_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=True, index=True)
    visibility = Column(Enum(MessageVisibility), nullable=False)
    scope = Column(Enum(MessageScope), nullable=False, default=MessageScope.PUBLIC, index=True)
    message_type = Column(String(32), nullable=False, default=CanonicalMessageType.SERVICE.value, index=True)
    message = Column(Text, nullable=False)
    # Quick-alert message templates are stored as rows in this table (one per
    # owner + message_type) flagged with `is_template=True`. They are the
    # pre-programmed bodies shown on the Settings page and are excluded from the
    # message feed / inbox queries.
    is_template = Column(Boolean, nullable=False, default=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    sender = relationship("Owner", foreign_keys=[sender_id], back_populates="sent_messages")
    receiver = relationship("Owner", foreign_keys=[receiver_id], back_populates="received_messages")

    __table_args__ = (
        Index("ix_message_sender_receiver", "sender_id", "receiver_id"),
        Index("ix_message_visibility_created", "visibility", "created_at"),
        Index("ix_message_type_created", "message_type", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Message(id={self.id}, sender_id={self.sender_id}, "
            f"receiver_id={self.receiver_id}, visibility={self.visibility})>"
        )
