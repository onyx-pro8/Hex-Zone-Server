"""Audit records for guest QR token lifecycle actions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String

from app.database import Base


class GuestAccessQrTokenAudit(Base):
    __tablename__ = "guest_access_qr_token_audits"

    id = Column(Integer, primary_key=True, index=True)
    token_id = Column(Integer, ForeignKey("guest_access_qr_tokens.id", ondelete="CASCADE"), nullable=False, index=True)
    zone_id = Column(String(100), nullable=False, index=True)
    action = Column(String(32), nullable=False, index=True)
    actor_owner_id = Column(Integer, ForeignKey("owners.id", ondelete="SET NULL"), nullable=True, index=True)
    reason = Column(String(255), nullable=True)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
