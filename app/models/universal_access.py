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


class UniversalAccessKind(str, enum.Enum):
    INVITE = "invite"
    PLACE_ACCESS = "place_access"


class UniversalAccessRequest(Base):
    """Network admin asks System Admin to invite someone, or to enter other zones."""

    __tablename__ = "universal_access_requests"

    id = Column(Integer, primary_key=True, index=True)
    network_admin_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    # Invite: the invited person. Place access: the requester (same as network_admin).
    member_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    member_name = Column(String(200), nullable=False)
    email = Column(String(255), nullable=False)
    address = Column(String(255), nullable=False, default="")
    photo_id = Column(Text, nullable=False, default="")
    business_document = Column(Text, nullable=False, default="")
    request_kind = Column(
        Enum(UniversalAccessKind),
        nullable=False,
        default=UniversalAccessKind.INVITE,
        index=True,
    )
    # Invite: zones the admin wants to share. Place access: empty until System Admin picks.
    zone_ids = Column(JSON, nullable=False, default=list)
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
    """Granted share. While enabled, the member sees the selected zones."""

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
    place_bbox = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
