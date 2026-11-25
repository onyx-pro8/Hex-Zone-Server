"""Short-lived password reset codes emailed through Resend."""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.database import Base


class PasswordReset(Base):
    """One active reset per email. The code is stored as a hash.

    After the code matches, ``reset_token_hash`` authorizes a single password
    change and the code can no longer be reused.
    """

    __tablename__ = "password_resets"

    id = Column(Integer, primary_key=True)
    # unique=True creates the unique index. Do not also set index=True.
    email = Column(String(255), unique=True, nullable=False)
    code_hash = Column(String(128), nullable=False, default="")
    reset_token_hash = Column(String(128), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    last_sent_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<PasswordReset(id={self.id}, email={self.email})>"
