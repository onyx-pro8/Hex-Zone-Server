"""Platform-wide zone area caps, set by the system administrator."""
from sqlalchemy import Column, Float, Integer

from app.database import Base


class ZoneSizeLimit(Base):
    """Single row. Null means that cap is not set."""

    __tablename__ = "zone_size_limits"

    id = Column(Integer, primary_key=True)
    each_zone_m2 = Column(Float, nullable=True)
    total_m2 = Column(Float, nullable=True)
