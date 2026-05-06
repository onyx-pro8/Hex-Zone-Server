"""Stored guest-door QR tokens (expiring, revocable); URL carries opaque secret (?gt=)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, text

from app.database import Base


class GuestAccessQrToken(Base):
    """
    Administrator-minted link segment embedded as SPA query **gt**.
    Lookup validates TTL, revocation, and optional max_uses (successful arrivals only).
    """

    __tablename__ = "guest_access_qr_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    zone_id = Column(String(100), nullable=False, index=True)
    event_id = Column(String(100), nullable=True, index=True)
    label = Column(String(255), nullable=True)

    created_by_owner_id = Column(Integer, ForeignKey("owners.id", ondelete="SET NULL"), nullable=False, index=True)

    expires_at = Column(DateTime, nullable=True, index=True)
    revoked_at = Column(DateTime, nullable=True, index=True)
    is_primary = Column(Boolean, nullable=False, default=False, server_default=text("0"), index=True)

    max_uses = Column(Integer, nullable=True)
    use_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_guest_access_qr_tokens_zone_created", "zone_id", "created_at"),
        Index(
            "ux_guest_access_qr_tokens_active_primary_zone",
            "zone_id",
            unique=True,
            sqlite_where=text("revoked_at IS NULL AND is_primary = 1"),
            postgresql_where=text("revoked_at IS NULL AND is_primary = TRUE"),
        ),
    )

    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        compare = now if now is not None else datetime.utcnow()
        return compare > self.expires_at

    def is_depleted(self) -> bool:
        if self.max_uses is None:
            return False
        return self.use_count >= self.max_uses
