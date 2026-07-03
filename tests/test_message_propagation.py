"""Message propagation: UNKNOWN global nearest + acceptable-zone delivery."""
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


def _mock_zone_id_fanout(
    monkeypatch,
    *,
    record_ids: list[int],
    zone_labels: list[str],
) -> None:
    def fake_resolve(
        db,
        *,
        latitude,
        longitude,
        exclude_owner_id=None,
        sender=None,
        network_zone_id=None,
        **_,
    ):
        pool = set(mfs._owner_ids_for_zone_id_labels(db, zone_labels))
        if exclude_owner_id is not None:
            pool.discard(int(exclude_owner_id))
        sorted_pool = sorted(pool)
        return (
            list(zone_labels),
            list(record_ids),
            sorted_pool,
            {
                "strategy": "primary_zone_network_members",
                "sender_zone_ids": list(zone_labels),
                "sender_zone_record_ids": list(record_ids),
                "recipient_owner_ids": sorted_pool,
            },
        )

    monkeypatch.setattr(mfs, "resolve_geo_propagation_recipient_owner_ids", fake_resolve)


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
    first_name: str = "T",
    last_name: str = "U",
    zone_id: str | None = None,
) -> Owner:
    owner = Owner(
        id=oid,
        email=email,
        zone_id=zone_id if zone_id is not None else f"zone-{oid}",
        first_name=first_name,
        last_name=last_name,
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


def test_resolve_unknown_origin_prefers_message_position():
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
    assert source == "message_position"
    assert lat == 40.0
    assert lon == -74.0


def test_resolve_unknown_origin_falls_back_to_owner_record():
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
        position=CoordinatePayload(latitude=40.7128, longitude=-74.0060),
    )
    payload.position.latitude = "bad"  # type: ignore[assignment]
    lat, lon, source = mfs._resolve_unknown_origin(sender, payload)
    assert source == "owner_record"
    assert lat == 48.8584
    assert lon == 2.2945


def test_resolve_unknown_origin_requires_coordinates_when_missing():
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
    payload.position.latitude = "bad"  # type: ignore[assignment]
    with pytest.raises(mfs.GeoMessageSkipped):
        mfs._resolve_unknown_origin(sender, payload)


def test_assert_unknown_rate_limit_accepts_owner_id_not_model(prop_db):
    """Regression: passing Owner into sender_id filter caused SQLAlchemy 500."""
    sender = _owner(prop_db, oid=7, email="rate@x.com", lat=0.0, lon=0.0)
    prop_db.commit()
    mfs._assert_unknown_rate_limit_ok(prop_db, sender.id)


def test_unknown_uses_message_position_when_owner_coords_missing(prop_db):
    network = "NET-UNK-1"
    sender = _owner(prop_db, oid=1, email="sender@x.com", lat=None, lon=None, zone_id=network)
    _owner(prop_db, oid=2, email="near@x.com", lat=40.7130, lon=-74.0060, zone_id=network)
    _owner(prop_db, oid=3, email="far@x.com", lat=50.0, lon=10.0, zone_id="OTHER-NET")
    prop_db.commit()

    payload = PropagationMessageCreate(
        type=MessageFeatureType.UNKNOWN,
        hid="device-1",
        position=CoordinatePayload(latitude=40.7128, longitude=-74.0060),
        msg={"description": "test"},
    )

    result = mfs.create_geo_propagated_message(prop_db, sender, payload)
    assert result["skipped"] is False
    assert result["fanout"]["strategy"] == "unknown_nearest_network"
    assert result["fanout"]["origin"]["source"] == "message_position"
    assert 2 in result["delivered_owner_ids"]
    assert 3 not in result["delivered_owner_ids"]
    prop_db.refresh(sender)
    assert sender.latitude == 40.7128
    assert sender.longitude == -74.0060


def test_unknown_nearest_network_respects_account_limit(prop_db):
    network = "NET-UNK-2"
    sender = _owner(
        prop_db,
        oid=1,
        email="sender@x.com",
        lat=0.0,
        lon=0.0,
        account_type=AccountType.EXCLUSIVE,
        zone_id=network,
    )
    _owner(prop_db, oid=2, email="n1@x.com", lat=0.01, lon=0.0, zone_id=network)
    _owner(prop_db, oid=3, email="n2@x.com", lat=0.02, lon=0.0, zone_id=network)
    _owner(prop_db, oid=4, email="n3@x.com", lat=0.03, lon=0.0, zone_id=network)
    _owner(prop_db, oid=5, email="n4@x.com", lat=0.04, lon=0.0, zone_id=network)
    _owner(prop_db, oid=6, email="n5@x.com", lat=0.05, lon=0.0, zone_id=network)
    _owner(prop_db, oid=7, email="n6@x.com", lat=0.06, lon=0.0, zone_id=network)
    prop_db.commit()

    payload = PropagationMessageCreate(
        type=MessageFeatureType.UNKNOWN,
        hid="device-1",
        position=CoordinatePayload(latitude=0.0, longitude=0.0),
        msg={"description": "test"},
    )

    result = mfs.create_geo_propagated_message(prop_db, sender, payload)
    assert result["fanout"]["strategy"] == "unknown_nearest_network"
    assert result["fanout"]["target_x"] == 5
    assert result["delivered_owner_ids"] == [2, 3, 4, 5, 6]


def test_unknown_delivers_nearest_on_same_network(prop_db):
    network = "NET-UNK-3"
    sender = _owner(prop_db, oid=1, email="sender@x.com", lat=0.0, lon=0.0, zone_id=network)
    _owner(prop_db, oid=2, email="near@x.com", lat=0.01, lon=0.0, zone_id=network)
    prop_db.commit()

    payload = PropagationMessageCreate(
        type=MessageFeatureType.UNKNOWN,
        hid="device-1",
        position=CoordinatePayload(latitude=0.0, longitude=0.0),
        msg={"description": "test"},
    )

    result = mfs.create_geo_propagated_message(prop_db, sender, payload)
    assert result["fanout"]["strategy"] == "unknown_nearest_network"
    assert result["delivered_owner_ids"] == [2]


def test_user_role_sender_reaches_zone_owner_when_sender_in_zone(prop_db, monkeypatch):
    zone_label = "zone-x"
    _owner(prop_db, oid=20, email="admin@x.com", lat=0.5, lon=0.5, zone_id=zone_label)
    user_sender = _owner(
        prop_db,
        oid=21,
        email="user@x.com",
        lat=0.0,
        lon=0.0,
        account_owner_id=20,
        role=OwnerRole.USER,
        zone_id=zone_label,
    )
    prop_db.commit()

    _mock_zone_id_fanout(
        monkeypatch,
        record_ids=[1],
        zone_labels=[zone_label],
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


def test_zone_propagation_delivers_to_owners_with_matching_zone_id(prop_db, monkeypatch):
    zone_label = "zone-match"
    sender = _owner(prop_db, oid=10, email="sender@x.com", lat=0.0, lon=0.0, zone_id=zone_label)
    _owner(prop_db, oid=11, email="inzone@x.com", lat=1.0, lon=1.0, zone_id=zone_label)
    _owner(prop_db, oid=99, email="outsider@x.com", lat=1.0, lon=1.0, zone_id="zone-other")
    prop_db.commit()

    _mock_zone_id_fanout(
        monkeypatch,
        record_ids=[42],
        zone_labels=[zone_label],
    )

    payload = PropagationMessageCreate(
        type=MessageFeatureType.PANIC,
        hid="device-1",
        position=CoordinatePayload(latitude=0.5, longitude=0.5),
        msg={"description": "panic"},
    )
    result = mfs.create_geo_propagated_message(prop_db, sender, payload)
    assert result["skipped"] is False
    assert result["fanout"]["strategy"] == "primary_zone_network_members"
    delivered = set(result["delivered_owner_ids"])
    assert 11 in delivered
    assert 99 not in delivered
    assert 10 not in delivered


def test_private_requires_sender_in_zone(prop_db, monkeypatch):
    zone_label = "zone-p"
    sender = _owner(
        prop_db,
        oid=30,
        email="psender@x.com",
        lat=0.5,
        lon=0.5,
        account_owner_id=30,
        zone_id=zone_label,
    )
    receiver = _owner(
        prop_db,
        oid=31,
        email="preceiver@x.com",
        lat=9.0,
        lon=9.0,
        account_owner_id=30,
        role=OwnerRole.USER,
        zone_id=zone_label,
    )
    prop_db.commit()

    _mock_zone_id_fanout(
        monkeypatch,
        record_ids=[1],
        zone_labels=[zone_label],
    )

    payload = PropagationMessageCreate(
        type=MessageFeatureType.PRIVATE,
        hid="device-1",
        position=CoordinatePayload(latitude=0.5, longitude=0.5),
        msg={"description": "dm"},
        receiver_owner_id=receiver.id,
    )
    result = mfs.create_geo_propagated_message(prop_db, sender, payload)
    assert result["skipped"] is False
    assert result["fanout"]["strategy"] == "private_sender_in_zone"
    assert set(result["delivered_owner_ids"]) == {31}


def test_private_rejects_when_sender_not_in_zone(prop_db, monkeypatch):
    sender = _owner(prop_db, oid=40, email="qsender@x.com", lat=0.5, lon=0.5, account_owner_id=40)
    receiver = _owner(
        prop_db,
        oid=41,
        email="qreceiver@x.com",
        lat=9.0,
        lon=9.0,
        account_owner_id=40,
        role=OwnerRole.USER,
    )
    prop_db.commit()

    monkeypatch.setattr(
        mfs,
        "resolve_geo_propagation_recipient_owner_ids",
        lambda db, *, latitude, longitude, exclude_owner_id=None, sender=None, network_zone_id=None, **_: (
            [],
            [],
            [],
            {"sender_zone_record_ids": []},
        ),
    )

    payload = PropagationMessageCreate(
        type=MessageFeatureType.PRIVATE,
        hid="device-1",
        position=CoordinatePayload(latitude=0.5, longitude=0.5),
        msg={"description": "dm"},
        receiver_owner_id=receiver.id,
    )
    with pytest.raises(mfs.PrivateScopeRecipientError):
        mfs.create_geo_propagated_message(prop_db, sender, payload)


def test_resolve_geo_propagation_matches_account_members_in_primary_zone(prop_db, monkeypatch):
    from types import SimpleNamespace

    from app.services import network_zone_propagation as nzp

    zone_label = "ZN-6DV321"
    admin = _owner(
        prop_db,
        oid=1,
        email="tester2@test.com",
        zone_id=zone_label,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
    )
    admin.account_owner_id = admin.id
    _owner(
        prop_db,
        oid=3,
        email="tester3@test.com",
        zone_id=zone_label,
        first_name="tester",
        last_name="3",
        role=OwnerRole.USER,
        account_owner_id=admin.id,
    )
    _owner(
        prop_db,
        oid=14,
        email="tester4@test.com",
        zone_id=zone_label,
        first_name="tester",
        last_name="4",
        role=OwnerRole.USER,
        account_owner_id=admin.id,
    )
    _owner(prop_db, oid=99, email="outsider@x.com", zone_id="OTHER-ZONE")
    prop_db.commit()

    monkeypatch.setattr(
        nzp,
        "evaluate_zone_records_containing_point",
        lambda db, lat, lon: [101],
    )
    monkeypatch.setattr(
        nzp,
        "_zone_rows_for_records",
        lambda db, ids: [
            SimpleNamespace(
                id=101,
                zone_id=zone_label,
                creator_id=admin.id,
                owner_id=admin.id,
                active=True,
            )
        ],
    )
    monkeypatch.setattr(
        nzp,
        "zone_ids_for_zone_records",
        lambda db, ids: [zone_label],
    )

    zone_ids, record_ids, recipients, meta = mfs.resolve_geo_propagation_recipient_owner_ids(
        prop_db,
        latitude=49.651,
        longitude=23.851,
        exclude_owner_id=admin.id,
        sender=admin,
    )
    assert zone_ids == [zone_label]
    assert record_ids == [101]
    assert set(recipients) == {3, 14}
    assert meta["strategy"] == "primary_zone_network_members"


def test_private_search_by_name(prop_db, monkeypatch):
    zone_label = "zone-s"
    sender = _owner(
        prop_db,
        oid=50,
        email="admin@x.com",
        lat=1.0,
        lon=1.0,
        account_owner_id=50,
        first_name="Admin",
        last_name="Root",
        zone_id=zone_label,
    )
    _owner(
        prop_db,
        oid=51,
        email="ann@x.com",
        lat=5.0,
        lon=5.0,
        account_owner_id=50,
        role=OwnerRole.USER,
        first_name="Ann",
        last_name="Johnson",
        zone_id=zone_label,
    )
    prop_db.commit()

    _mock_zone_id_fanout(
        monkeypatch,
        record_ids=[99],
        zone_labels=[zone_label],
    )

    result = mfs.search_private_message_recipients(prop_db, sender, "ann")
    assert result["zone_ids"] == [zone_label]
    assert result["location_status"] == "inside_zone"
    assert len(result["members"]) == 1
    assert result["members"][0]["id"] == 51
    assert "Ann" in result["members"][0]["display_name"]


def test_private_search_outside_zone(prop_db, monkeypatch):
    zone_label = "zone-solo"
    sender = _owner(
        prop_db,
        oid=60,
        email="solo@x.com",
        lat=1.0,
        lon=1.0,
        role=OwnerRole.USER,
        account_owner_id=None,
        zone_id=zone_label,
    )
    prop_db.commit()

    monkeypatch.setattr(
        mfs,
        "resolve_geo_propagation_recipient_owner_ids",
        lambda db, *, latitude, longitude, exclude_owner_id=None, sender=None, network_zone_id=None, **_: (
            [],
            [],
            [],
            {"sender_zone_record_ids": []},
        ),
    )

    result = mfs.search_private_message_recipients(prop_db, sender, "ann")
    assert result["zone_ids"] == []
    assert result["members"] == []
    assert result["location_status"] == "outside_zone"
