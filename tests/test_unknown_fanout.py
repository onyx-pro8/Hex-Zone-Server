"""UNKNOWN nearest-neighbour fan-out and alarm push helpers."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.domain.message_types import is_alarm_push_type
from app.services.unknown_fanout_service import (
    FANOUT_LIMIT_BY_ACCOUNT_TYPE,
    resolve_nearest_owner_ids,
    unknown_fanout_limit,
)


@pytest.fixture()
def fanout_db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()


def _owner(
    db,
    *,
    oid: int,
    email: str,
    lat: float | None,
    lon: float | None,
    account_type: AccountType = AccountType.PRIVATE,
) -> Owner:
    owner = Owner(
        id=oid,
        email=email,
        zone_id=f"zone-{oid}",
        first_name="T",
        last_name="U",
        account_type=account_type,
        role=OwnerRole.USER,
        hashed_password="x",
        api_key=f"key-{oid}",
        address="addr",
        latitude=lat,
        longitude=lon,
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(owner)
    db.flush()
    return owner


def test_fanout_limits_match_product_table():
    assert FANOUT_LIMIT_BY_ACCOUNT_TYPE["exclusive"] == 5
    assert FANOUT_LIMIT_BY_ACCOUNT_TYPE["enhanced_plus"] == 1000


def test_unknown_fanout_limit_for_sender(fanout_db):
    sender = _owner(fanout_db, oid=1, email="a@x.com", lat=0.0, lon=0.0, account_type=AccountType.PRIVATE_PLUS)
    assert unknown_fanout_limit(sender) == 50


def test_resolve_nearest_owner_ids_orders_by_distance(fanout_db):
    sender = _owner(fanout_db, oid=1, email="sender@x.com", lat=0.0, lon=0.0)
    _owner(fanout_db, oid=2, email="near@x.com", lat=0.01, lon=0.0)
    _owner(fanout_db, oid=3, email="far@x.com", lat=1.0, lon=1.0)
    _owner(fanout_db, oid=4, email="mid@x.com", lat=0.1, lon=0.0)

    nearest = resolve_nearest_owner_ids(
        fanout_db,
        origin_lat=0.0,
        origin_lon=0.0,
        sender_id=sender.id,
        limit=2,
    )
    assert nearest == [2, 4]


def test_resolve_nearest_excludes_sender_and_null_coords(fanout_db):
    sender = _owner(fanout_db, oid=10, email="s@x.com", lat=0.0, lon=0.0)
    _owner(fanout_db, oid=11, email="ok@x.com", lat=0.02, lon=0.0)
    _owner(fanout_db, oid=12, email="noloc@x.com", lat=None, lon=None)

    nearest = resolve_nearest_owner_ids(
        fanout_db,
        origin_lat=0.0,
        origin_lon=0.0,
        sender_id=sender.id,
        limit=10,
    )
    assert 10 not in nearest
    assert 12 not in nearest
    assert nearest == [11]


def test_alarm_push_types():
    assert is_alarm_push_type("UNKNOWN")
    assert is_alarm_push_type("PANIC")
    assert is_alarm_push_type("NS_PANIC")
    assert is_alarm_push_type("SENSOR")
    assert not is_alarm_push_type("CHAT")
