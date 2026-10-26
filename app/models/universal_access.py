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
    UniqueConstraint,
)

from app.database import Base


class UniversalAccessStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class UniversalAccessKind(str, enum.Enum):
    INVITE = "invite"
    PLACE_ACCESS = "place_access"


class UniversalAccessKeyType(str, enum.Enum):
    PRIVATE = "private"
    PUBLIC = "public"


class UniversalAccessRequest(Base):
    """Invite key generation, or a place-access request to a network admin."""

    __tablename__ = "universal_access_requests"

    id = Column(Integer, primary_key=True, index=True)
    # Invite: the network admin who generated the key.
    # Place access: the network admin who must approve/reject.
    network_admin_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    # Invite private: the invited person. Invite public: the generating admin.
    # Place access: the requester.
    member_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    member_name = Column(String(200), nullable=False)
    email = Column(String(255), nullable=False)
    address = Column(String(255), nullable=False, default="")
    photo_id = Column(Text, nullable=False, default="")
    business_document = Column(Text, nullable=False, default="")
    request_kind = Column(
        Enum(
            UniversalAccessKind,
            native_enum=False,
            length=32,
            values_callable=lambda choices: [item.value for item in choices],
        ),
        nullable=False,
        default=UniversalAccessKind.INVITE,
        server_default=UniversalAccessKind.INVITE.value,
        index=True,
    )
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
    """Granted share. While enabled, members (or public-key holders) see the zones."""

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
    key_type = Column(
        Enum(
            UniversalAccessKeyType,
            native_enum=False,
            length=16,
            values_callable=lambda choices: [item.value for item in choices],
        ),
        nullable=False,
        default=UniversalAccessKeyType.PRIVATE,
        server_default=UniversalAccessKeyType.PRIVATE.value,
        index=True,
    )
    zone_ids = Column(JSON, nullable=False, default=list)
    country = Column(String(120), nullable=False, default="")
    region = Column(String(120), nullable=False, default="")
    city = Column(String(120), nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    place_bbox = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class UniversalAccessActivation(Base):
    """Public key entered by a user so those zones appear on their map."""

    __tablename__ = "universal_access_activations"
    __table_args__ = (
        UniqueConstraint("credential_id", "owner_id", name="uq_universal_access_activation"),
    )

    id = Column(Integer, primary_key=True, index=True)
    credential_id = Column(
        Integer,
        ForeignKey("universal_access_credentials.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
