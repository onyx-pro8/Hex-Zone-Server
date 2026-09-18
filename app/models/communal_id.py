"""Registry of public Communal IDs minted by network administrators."""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Index
from sqlalchemy.orm import relationship

from app.database import Base


class CommunalIdRegistry(Base):
    """Public Communal ID owned by the admin who generated it.

    Zones tagged with this ID become visible to every member of the creator's
    network without counting toward that network's zone quota.
    """

    __tablename__ = "communal_id_registry"

    id = Column(Integer, primary_key=True, index=True)
    reference_id = Column(String(32), unique=True, nullable=False, index=True)
    creator_id = Column(
        Integer,
        ForeignKey("owners.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Snapshot of the creator's network id (owners.zone_id) at mint time.
    network_id = Column(String(100), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    creator = relationship("Owner", foreign_keys=[creator_id])

    __table_args__ = (
        Index("ix_communal_id_registry_network_ref", "network_id", "reference_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<CommunalIdRegistry(id={self.id}, reference_id={self.reference_id!r}, "
            f"network_id={self.network_id!r})>"
        )
