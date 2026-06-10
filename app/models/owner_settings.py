"""Per-owner application settings.

Stores the broadcast identity, structured home address, shared-notification
integration, and pre-programmed quick-alert messages shown on the client
Settings page. These were previously persisted only in browser localStorage,
so they did not survive a new device / cleared browser. This table makes them
account-scoped and durable.
"""
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String

from app.database import Base


class OwnerSettings(Base):
    __tablename__ = "owner_settings"

    owner_id = Column(
        Integer,
        ForeignKey("owners.id", ondelete="CASCADE"),
        primary_key=True,
    )

    broadcast_name = Column(String(255), nullable=False, default="")

    # Structured home address (distinct from owners.address, which is the single
    # free-form string captured at signup and used for geocoding / zone resolution).
    address_number_street = Column(String(255), nullable=False, default="")
    address_street_name = Column(String(255), nullable=False, default="")
    address_city = Column(String(255), nullable=False, default="")
    address_state_province = Column(String(255), nullable=False, default="")
    address_city_code = Column(String(64), nullable=False, default="")

    # sharednotification.com integration settings.
    sn_hid = Column(String(255), nullable=False, default="")
    sn_network_id = Column(String(255), nullable=False, default="")
    sn_api_key = Column(String(255), nullable=False, default="")
    sn_webhook = Column(String(255), nullable=False, default="/alertname")
    sn_periodical_check_sec = Column(String(32), nullable=False, default="86400")

    # Pre-programmed quick-alert message bodies keyed by message type.
    quick_messages = Column(JSON, nullable=False, default=dict)

    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<OwnerSettings(owner_id={self.owner_id})>"
