"""QR Registration model."""
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Boolean, Index
from sqlalchemy.orm import relationship
from datetime import datetime, timedelta
from app.database import Base


class QRRegistration(Base):
    """QR Registration token model."""
    __tablename__ = "qr_registrations"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(255), unique=True, nullable=False, index=True)
    
    # Owner reference (administrator who issued the member invite)
    owner_id = Column(Integer, ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Status
    used = Column(Boolean, default=False, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    owner = relationship("Owner", back_populates="qr_registrations")

    __table_args__ = (
        Index("ix_qr_token", "token"),
        Index("ix_qr_owner_id", "owner_id"),
    )

    def is_expired(self) -> bool:
        """Check if QR registration token is expired."""
        return datetime.utcnow() > self.expires_at

    def __repr__(self) -> str:
        return f"<QRRegistration(id={self.id}, owner_id={self.owner_id}, used={self.used})>"
