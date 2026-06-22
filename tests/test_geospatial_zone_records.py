"""Geo fan-out uses exact zone row geometry, not shared zone_id labels."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Owner, Zone
from app.models.owner import AccountType, OwnerRole
from app.models.zone import ZoneType
from app.schemas.message_feature import CoordinatePayload, MessageFeatureType, PropagationMessageCreate
from app.services import message_feature_service as mfs
from app.services.geospatial_service import (
    evaluate_zone_records_containing_point,
    owner_ids_located_within_zone_ids,
    owner_ids_located_within_zone_records,
)


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


def _owner(db, *, oid: int, email: str, zone_id: str, lat: float, lon: float) -> Owner:
    owner = Owner(
        id=oid,
        email=email,
        zone_id=zone_id,
        first_name="T",
        last_name="U",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR if oid == 1 else OwnerRole.USER,
        account_owner_id=1 if oid != 1 else None,
        hashed_password="x",
        api_key=f"key-{oid}",
        address="addr",
        latitude=lat,
        longitude=lon,
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    if oid == 1:
        owner.account_owner_id = 1
    db.add(owner)
    db.flush()
    return owner


def _proximity_zone(
    db,
    *,
    record_id: int,
    owner_id: int,
    zone_id: str,
    center_lat: float,
    center_lon: float,
    radius_meters: float = 8000.0,
) -> Zone:
    zone = Zone(
        id=record_id,
        zone_id=zone_id,
        owner_id=owner_id,
        creator_id=owner_id,
        zone_type=ZoneType.GEOFENCE,
        name=f"zone-{record_id}",
        parameters={
            "contractType": "proximity",
            "geometry": {"center": {"latitude": center_lat, "longitude": center_lon}},
            "config": {"radius_meters": radius_meters},
        },
        h3_cells=[],
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(zone)
    db.flush()
    return zone


def test_shared_zone_id_does_not_fan_out_to_other_geometries(geo_db):
    """Regression: Seattle send must not notify Miami member on same zone_id label."""
    shared = "FAMILY-ACCOUNT"
    sender = _owner(
        geo_db,
        oid=1,
        email="sender@x.com",
        zone_id=shared,
        lat=47.6062,
        lon=-122.3321,
    )
    seattle_peer = _owner(
        geo_db,
        oid=2,
        email="seattle@x.com",
        zone_id=shared,
        lat=47.6070,
        lon=-122.3330,
    )
    miami_peer = _owner(
        geo_db,
        oid=3,
        email="miami@x.com",
        zone_id=shared,
        lat=25.7617,
        lon=-80.1918,
    )
    seattle_zone = _proximity_zone(
        geo_db,
        record_id=101,
        owner_id=sender.id,
        zone_id=shared,
        center_lat=47.6062,
        center_lon=-122.3321,
    )
    _proximity_zone(
        geo_db,
        record_id=102,
        owner_id=miami_peer.id,
        zone_id=shared,
        center_lat=25.7617,
        center_lon=-80.1918,
    )
    geo_db.commit()

    send_lat, send_lon = 47.6062, -122.3321
    matched_records = evaluate_zone_records_containing_point(geo_db, send_lat, send_lon)
    assert matched_records == [seattle_zone.id]

    by_records = owner_ids_located_within_zone_records(
        geo_db, matched_records, exclude_owner_id=sender.id
    )
    assert by_records == [seattle_peer.id]

    legacy_by_label = owner_ids_located_within_zone_ids(geo_db, [shared], exclude_owner_id=sender.id)
    assert miami_peer.id in legacy_by_label

    payload = PropagationMessageCreate(
        type=MessageFeatureType.PANIC,
        hid="device-1",
        position=CoordinatePayload(latitude=send_lat, longitude=send_lon),
        msg={"description": "panic"},
    )
    result = mfs.create_geo_propagated_message(geo_db, sender, payload)
    delivered = set(result["delivered_owner_ids"])
    assert seattle_peer.id in delivered
    assert miami_peer.id not in delivered
    assert sender.id not in delivered
