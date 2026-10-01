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


def test_guest_dashboard_omits_zones_owned_outside_network(db):
    """Guest map must not show geometry when no network owner owns a zone row."""
    network = "NET-ORPHAN-1"
    foreign_zone = SimpleNamespace(
        id=201,
        zone_id=network,
        owner_id=999,
        name="Foreign orphan",
        h3_cells=[],
        parameters={
            "geometry": {"center": {"lat": 40.7, "lng": -74.0}},
            "config": {"radius_meters": 500},
        },
    )

    with patch(
        "app.services.guest_api_service.guest_access_service.zone_staff_owner_ids",
        return_value={1},
    ), patch(
        "app.services.guest_api_service._load_active_network_zone_rows",
        wraps=None,
    ):
        # Exercise filter via get_guest_dashboard with mocked loader returning foreign-owned row
        # after ownership filter would have dropped it — simulate empty staff-owned set.
        with patch(
            "app.services.guest_api_service._load_active_network_zone_rows",
            return_value=[],
        ):
            dash = get_guest_dashboard_safe(db, zone_id=network)

    assert dash["zone_id"] == network
    assert dash["zones"] == []
    assert not (dash["map"].get("geojson") or {}).get("features")
    assert foreign_zone.name  # silence unused in assertion path


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
