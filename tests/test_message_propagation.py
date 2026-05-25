"""Message propagation: account union + acceptable-zone + UNKNOWN origin."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.domain.message_types import CanonicalMessageType
from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.schemas.message_feature import CoordinatePayload, MessageFeatureType, PropagationMessageCreate
from app.services import message_feature_service as mfs


@pytest.fixture()
def prop_db():
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
    lat: float | None = None,
    lon: float | None = None,
    account_type: AccountType = AccountType.EXCLUSIVE,
    role: OwnerRole = OwnerRole.ADMINISTRATOR,
    account_owner_id: int | None = None,
) -> Owner:
    owner = Owner(
        id=oid,
        email=email,
        zone_id=f"zone-{oid}",
        first_name="T",
        last_name="U",
        account_type=account_type,
        role=role,
        account_owner_id=account_owner_id,
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


def test_merge_propagation_recipients_unions_and_excludes_sender():
    result = mfs._merge_propagation_recipients(
        sender_id=1,
        account_owner_ids=[1, 2, 3],
        acceptable_zone_owner_ids=[3, 4, 5],
    )
    assert result == [2, 3, 4, 5]


def test_resolve_unknown_origin_prefers_owner_record():
    sender = Owner(
        id=1,
        email="a@x.com",
        zone_id="z",
        first_name="A",
        last_name="B",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.ADMINISTRATOR,
        hashed_password="x",
        api_key="k",
        address="addr",
        latitude=48.8584,
        longitude=2.2945,
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    payload = PropagationMessageCreate(
        type=MessageFeatureType.UNKNOWN,
        hid="hid",
        position=CoordinatePayload(latitude=40.0, longitude=-74.0),
    )
    lat, lon, source = mfs._resolve_unknown_origin(sender, payload)
    assert source == "owner_record"
    assert lat == 48.8584
    assert lon == 2.2945


def test_resolve_unknown_origin_falls_back_to_message_position():
    sender = Owner(
        id=1,
        email="a@x.com",
        zone_id="z",
        first_name="A",
        last_name="B",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.ADMINISTRATOR,
        hashed_password="x",
        api_key="k",
        address="addr",
        latitude=None,
        longitude=None,
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    payload = PropagationMessageCreate(
        type=MessageFeatureType.UNKNOWN,
        hid="hid",
        position=CoordinatePayload(latitude=40.7128, longitude=-74.0060),
    )
    lat, lon, source = mfs._resolve_unknown_origin(sender, payload)
    assert source == "message_position"
    assert lat == 40.7128
    assert lon == -74.0060


def test_assert_unknown_rate_limit_accepts_owner_id_not_model(prop_db):
    """Regression: passing Owner into sender_id filter caused SQLAlchemy 500."""
    sender = _owner(prop_db, oid=7, email="rate@x.com", lat=0.0, lon=0.0)
    prop_db.commit()
    mfs._assert_unknown_rate_limit_ok(prop_db, sender.id)


def test_unknown_uses_message_position_when_owner_coords_missing(prop_db, monkeypatch):
    sender = _owner(prop_db, oid=1, email="sender@x.com", lat=None, lon=None)
    _owner(prop_db, oid=2, email="near@x.com", lat=40.7130, lon=-74.0060)
    _owner(prop_db, oid=3, email="far@x.com", lat=50.0, lon=10.0)
    prop_db.commit()

    payload = PropagationMessageCreate(
        type=MessageFeatureType.UNKNOWN,
        hid="device-1",
        position=CoordinatePayload(latitude=40.7128, longitude=-74.0060),
        msg={"description": "test"},
    )

    result = mfs.create_geo_propagated_message(prop_db, sender, payload)
    assert result["skipped"] is False
    assert result["fanout"]["strategy"] == "unknown_nearest"
    assert result["fanout"]["origin"]["source"] == "message_position"
    assert 2 in result["delivered_owner_ids"]
    prop_db.refresh(sender)
    assert sender.latitude == 40.7128
    assert sender.longitude == -74.0060


def test_user_role_sender_reaches_same_account_members(prop_db, monkeypatch):
    admin = _owner(prop_db, oid=20, email="admin@x.com", lat=0.0, lon=0.0)
    user_sender = _owner(
        prop_db,
        oid=21,
        email="user@x.com",
        lat=0.0,
        lon=0.0,
        account_owner_id=20,
        role=OwnerRole.USER,
    )

    monkeypatch.setattr(
        mfs,
        "owner_ids_whose_acceptable_zones_contain_point",
        lambda db, latitude, longitude: ([], []),
    )

    payload = PropagationMessageCreate(
        type=MessageFeatureType.PANIC,
        hid="device-1",
        position=CoordinatePayload(latitude=0.5, longitude=0.5),
        msg={"description": "user panic"},
    )
    result = mfs.create_geo_propagated_message(prop_db, user_sender, payload)
    assert result["skipped"] is False
    delivered = set(result["delivered_owner_ids"])
    assert 20 in delivered
    assert 21 not in delivered


def test_zone_propagation_merges_account_and_acceptable_zone_owners(prop_db, monkeypatch):
    sender = _owner(prop_db, oid=10, email="sender@x.com", lat=0.0, lon=0.0)
    _owner(prop_db, oid=11, email="member@x.com", account_owner_id=10, role=OwnerRole.USER)
    _owner(prop_db, oid=99, email="outsider@x.com", lat=1.0, lon=1.0)

    def fake_zone_owners(db, latitude, longitude):
        assert latitude == 0.5
        assert longitude == 0.5
        return ["zone-match"], [99]

    monkeypatch.setattr(
        mfs,
        "owner_ids_whose_acceptable_zones_contain_point",
        fake_zone_owners,
    )

    payload = PropagationMessageCreate(
        type=MessageFeatureType.PANIC,
        hid="device-1",
        position=CoordinatePayload(latitude=0.5, longitude=0.5),
        msg={"description": "panic"},
    )
    result = mfs.create_geo_propagated_message(prop_db, sender, payload)
    assert result["skipped"] is False
    assert result["fanout"]["strategy"] == "account_plus_acceptable_zone"
    delivered = set(result["delivered_owner_ids"])
    assert 11 in delivered
    assert 99 in delivered
    assert 10 not in delivered
