"""Short-lived signup held until the email verification code is confirmed."""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base


class PendingSignup(Base):
    """Registration payload waiting on a 4-digit email code.

    The password is stored only as a hash. ``payload_json`` never includes it.
    """

    __tablename__ = "pending_signups"

    id = Column(Integer, primary_key=True)
    # unique=True creates the unique index. Do not also set index=True — that
    # duplicates the index name and breaks PostgreSQL create_all().
    email = Column(String(255), unique=True, nullable=False)
    code_hash = Column(String(128), nullable=False)
    payload_json = Column(Text, nullable=False)
    password_hash = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    last_sent_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<PendingSignup(id={self.id}, email={self.email})>"
