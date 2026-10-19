"""Zone area caps, set by the system administrator per account type and role."""
from sqlalchemy import JSON, Column, Float, Integer

from app.database import Base


class ZoneSizeLimit(Base):
    """Single row. ``rules`` holds one cap pair per account type and role."""

    __tablename__ = "zone_size_limits"

    id = Column(Integer, primary_key=True)
    each_zone_m2 = Column(Float, nullable=True)
    total_m2 = Column(Float, nullable=True)
    rules = Column(JSON, nullable=True)
