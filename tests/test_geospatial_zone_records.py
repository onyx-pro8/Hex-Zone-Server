"""Geospatial zone-record helpers."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import pytest

from app.database import Base
from app.services.geospatial_service import evaluate_zone_records_containing_point


@pytest.fixture()
def geo_db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()


def test_evaluate_zone_records_empty_without_zones(geo_db):
    assert evaluate_zone_records_containing_point(geo_db, 47.6, -122.3) == []
