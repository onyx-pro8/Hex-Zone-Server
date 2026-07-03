"""Guest dashboard returns every acceptable zone in a network."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.services.guest_api_service import get_guest_dashboard_safe


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_guest_dashboard_lists_all_network_zones(db):
    network = "NET-ZONES-1"
    admin_zone = SimpleNamespace(
        id=101,
        zone_id=network,
        owner_id=1,
        name="Admin primary",
        h3_cells=[],
        parameters={
            "geometry": {"center": {"lat": 40.7, "lng": -74.0}},
            "config": {"radius_meters": 500},
        },
    )
    member_zone = SimpleNamespace(
        id=102,
        zone_id=network,
        owner_id=2,
        name="Member secondary",
        h3_cells=[],
        parameters={
            "geometry": {"center": {"lat": 40.8, "lng": -73.9}},
            "config": {"radius_meters": 400},
        },
    )

    with patch(
        "app.services.guest_api_service._load_active_network_zone_rows",
        return_value=[(admin_zone, None), (member_zone, None)],
    ):
        dash = get_guest_dashboard_safe(db, zone_id=network)

    assert dash["zone_id"] == network
    assert len(dash["zones"]) == 2
    names = {z["name"] for z in dash["zones"]}
    assert names == {"Admin primary", "Member secondary"}

    gj = dash["map"].get("geojson")
    assert isinstance(gj, dict)
    assert gj.get("type") == "FeatureCollection"
    assert len(gj.get("features") or []) == 2
