"""Universal access requests and the credentials minted from them."""
from datetime import datetime
import enum

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)

from app.database import Base


class UniversalAccessStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class UniversalAccessRequest(Base):
    """A network admin asks the system admin to share selected primary zones."""

    __tablename__ = "universal_access_requests"

    id = Column(Integer, primary_key=True, index=True)
    network_admin_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    member_name = Column(String(200), nullable=False)
    email = Column(String(255), nullable=False)
    address = Column(String(255), nullable=False, default="")
    photo_id = Column(Text, nullable=False)
    business_document = Column(Text, nullable=False, default="")
    # Integer primary-zone ids the admin is offering to share.
    zone_ids = Column(JSON, nullable=False, default=list)
    # Kept for older rows. New requests do not use a place.
    country = Column(String(120), nullable=False, default="")
    region = Column(String(120), nullable=False, default="")
    city = Column(String(120), nullable=False, default="")
    note = Column(Text, nullable=False, default="")
    status = Column(
        Enum(UniversalAccessStatus),
        nullable=False,
        default=UniversalAccessStatus.PENDING,
    )
    reject_reason = Column(Text, nullable=False, default="")
    reviewed_by_id = Column(Integer, ForeignKey("owners.id", ondelete="SET NULL"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class UniversalAccessCredential(Base):
    """Official credential. While enabled, the invitee sees the shared primary zones."""

    __tablename__ = "universal_access_credentials"

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(
        Integer,
        ForeignKey("universal_access_requests.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    member_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    network_admin_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    code = Column(String(32), nullable=False, unique=True, index=True)
    zone_ids = Column(JSON, nullable=False, default=list)
    country = Column(String(120), nullable=False, default="")
    region = Column(String(120), nullable=False, default="")
    city = Column(String(120), nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    # Legacy place box. New credentials share explicit zone ids instead.
    place_bbox = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
