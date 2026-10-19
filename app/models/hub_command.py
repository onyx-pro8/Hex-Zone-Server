"""Record of a command a network admin sent to their smart hub."""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class HubCommand(Base):
    __tablename__ = "hub_commands"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    hid = Column(String(255), nullable=False)
    command = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False)
    detail = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
