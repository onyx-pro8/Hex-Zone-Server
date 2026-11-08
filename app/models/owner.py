"""Owner/User model."""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum, Float, ForeignKey, Index, Text, event
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
import re
import uuid
from app.database import Base


class AccountType(str, enum.Enum):
    """Account type enumeration."""
    PRIVATE = "private"
    PRIVATE_PLUS = "private_plus"
    EXCLUSIVE = "exclusive"
    ENHANCED = "enhanced"
    ENHANCED_PLUS = "enhanced_plus"


class OwnerRole(str, enum.Enum):
    """Owner role enumeration."""
    ADMINISTRATOR = "administrator"
    USER = "user"


class Owner(Base):
    """Owner/User model."""
    __tablename__ = "owners"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    # Public unique handle (lowercase letters, digits, underscore). Never treat as login email.
    username = Column(String(64), unique=True, nullable=False, index=True, default="")
    zone_id = Column(String(100), nullable=False, index=True)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    # Optional display name for outgoing messages. When blank, clients and the
    # API fall back to ``first_name`` + ``last_name``. Distinct from the
    # account identity fields — never auto-filled from them.
    broadcast_name = Column(String(255), nullable=False, default="")
    # Smart-home integration settings that are owner-scoped and editable.
    # HID shown on Settings is selected among the owner's smart-home hubs and
    # stored in ``sn_hid``. API key / network id remain derived from
    # ``api_key`` / ``zone_id``.
    sn_webhook = Column(String(255), nullable=False, default="")
    sn_periodical_check_sec = Column(String(32), nullable=False, default="86400")
    sn_hid = Column(String(255), nullable=False, default="")
    # Optional template for the SERVICE welcome posted when a member joins.
    # Placeholders: {member_name}/{member name}, {network_name}/{network name},
    # {first_name}, {last_name}. Blank → server default.
    member_join_welcome = Column(Text, nullable=False, default="")
    account_type = Column(Enum(AccountType), nullable=False, default=AccountType.PRIVATE)
    # Organization (enhanced_plus) capacity band 1–5; null for other tiers.
    tier_level = Column(Integer, nullable=True)
    role = Column(Enum(OwnerRole), nullable=False, default=OwnerRole.ADMINISTRATOR)
    account_owner_id = Column(Integer, ForeignKey("owners.id", ondelete="SET NULL"), nullable=True, index=True)
    hashed_password = Column(String(255), nullable=False)
    api_key = Column(String(255), unique=True, nullable=False, index=True)
    phone = Column(String(20), nullable=True)
    address = Column(String(255), nullable=False)
    # Optional profile image as a URL or data URI (set from User settings).
    avatar_url = Column(Text, nullable=True)
    # Legacy column: Communal IDs are no longer issued to Individuals/members.
    # Network admins mint public IDs into communal_id_registry instead.
    communal_id = Column(String(32), nullable=True, index=True)
    # Canonical owner home location (geocoded from `address`) for SENSOR /
    # WELLNESS_CHECK routing and client `mapCenter`. Live GPS is in
    # `member_locations`, not mirrored here.
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    location_updated_at = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True, nullable=False)
    expired = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    devices = relationship("Device", back_populates="owner", cascade="all, delete-orphan")
    zones = relationship(
        "Zone",
        back_populates="owner",
        cascade="all, delete-orphan",
        foreign_keys="Zone.owner_id",
    )
    created_zones = relationship(
        "Zone",
        foreign_keys="Zone.creator_id",
    )
    qr_registrations = relationship("QRRegistration", back_populates="owner", cascade="all, delete-orphan")
    sent_messages = relationship(
        "Message",
        foreign_keys="Message.sender_id",
        back_populates="sender",
        cascade="all, delete-orphan",
    )
    received_messages = relationship(
        "Message",
        foreign_keys="Message.receiver_id",
        back_populates="receiver",
    )
    account_owner = relationship("Owner", remote_side=[id], foreign_keys=[account_owner_id], post_update=True)

    __table_args__ = (
        Index("ix_owner_email", "email"),
        Index("ix_owner_username", "username"),
        Index("ix_owner_zone_id", "zone_id"),
        Index("ix_owner_api_key", "api_key"),
    )

    @property
    def message_display_name(self) -> str:
        """Name shown on messages: custom broadcast name, else first + last."""
        configured = (self.broadcast_name or "").strip()
        if configured:
            return configured
        full = f"{self.first_name} {self.last_name}".strip()
        return full or (self.username or "").strip() or "Member"

    def __repr__(self) -> str:
        return (
            f"<Owner(id={self.id}, username={self.username}, "
            f"account_type={self.account_type})>"
        )


@event.listens_for(Owner, "before_insert")
def _assign_username_before_insert(mapper, connection, target: Owner) -> None:
    """Guarantee a unique username for every new owner row (tests/seeds included).

    Tries ``firstname`` + ``lastname`` + ``1``, then ``2``, and so on.
    """
    from sqlalchemy import text

    current = (getattr(target, "username", None) or "").strip().lower()
    if current:
        target.username = current
        return
    first = re.sub(r"[^a-z0-9]", "", (target.first_name or "").lower())
    last = re.sub(r"[^a-z0-9]", "", (target.last_name or "").lower())
    base = (f"{first}{last}" or "user")[:58]
    for n in range(1, 100_000):
        candidate = f"{base}{n}"[:64]
        exists = connection.execute(
            text(
                "SELECT 1 FROM owners WHERE lower(username) = lower(:username) LIMIT 1"
            ),
            {"username": candidate},
        ).first()
        if not exists:
            target.username = candidate
            return
    target.username = f"user{uuid.uuid4().hex[:8]}"
