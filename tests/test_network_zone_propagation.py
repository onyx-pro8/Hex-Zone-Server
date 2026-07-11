"""Primary vs secondary acceptable-zone propagation scenarios."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.schemas.message_feature import CoordinatePayload, MessageFeatureType, PropagationMessageCreate
from app.services import message_feature_service as mfs
from app.services import network_zone_propagation as nzp


@pytest.fixture()
def net_db():
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
    network_id: str,
    role: OwnerRole,
    account_owner_id: int | None,
    lat: float,
    lon: float,
    account_type: AccountType = AccountType.PRIVATE,
) -> Owner:
    owner = Owner(
        id=oid,
        email=email,
        zone_id=network_id,
        first_name="T",
        last_name=str(oid),
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


def _zone_row(*, record_id: int, network_id: str, creator_id: int, owner_id: int):
    return SimpleNamespace(
        id=record_id,
        zone_id=network_id,
        creator_id=creator_id,
        owner_id=owner_id,
        active=True,
    )


def test_primary_zone_delivers_to_admin_and_invited_members(net_db, monkeypatch):
    network = "NET-1"
    admin = _owner(
        net_db,
        oid=1,
        email="admin@x.com",
        network_id=network,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=47.6062,
        lon=-122.3321,
    )
    admin.account_owner_id = admin.id
    member = _owner(
        net_db,
        oid=2,
        email="member@x.com",
        network_id=network,
        role=OwnerRole.USER,
        account_owner_id=admin.id,
        lat=25.0,
        lon=-80.0,
    )
    net_db.commit()
    monkeypatch.setattr(
        nzp,
        "evaluate_zone_records_containing_point",
        lambda db, lat, lon: [101],
    )
    monkeypatch.setattr(
        nzp,
        "zone_ids_for_zone_records",
        lambda db, ids: [network],
    )
    monkeypatch.setattr(
        nzp,
        "_zone_rows_for_records",
        lambda db, ids: [_zone_row(record_id=101, network_id=network, creator_id=admin.id, owner_id=admin.id)],
    )

    _, _, recipients, meta = nzp.resolve_network_geo_propagation_recipients(
        net_db,
        admin,
        latitude=47.6062,
        longitude=-122.3321,
        exclude_owner_id=admin.id,
    )
    assert meta["strategy"] == "primary_zone_network_members"
    assert set(recipients) == {member.id}


def test_secondary_zone_delivers_only_to_creator(net_db, monkeypatch):
    network = "NET-2"
    admin = _owner(
        net_db,
        oid=1,
        email="admin@x.com",
        network_id=network,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=47.6062,
        lon=-122.3321,
    )
    admin.account_owner_id = admin.id
    member = _owner(
        net_db,
        oid=2,
        email="member@x.com",
        network_id=network,
        role=OwnerRole.USER,
        account_owner_id=admin.id,
        lat=47.6070,
        lon=-122.3330,
    )
    net_db.commit()
    monkeypatch.setattr(
        nzp,
        "evaluate_zone_records_containing_point",
        lambda db, lat, lon: [201],
    )
    monkeypatch.setattr(
        nzp,
        "zone_ids_for_zone_records",
        lambda db, ids: [network],
    )
    monkeypatch.setattr(
        nzp,
        "_zone_rows_for_records",
        lambda db, ids: [_zone_row(record_id=201, network_id=network, creator_id=member.id, owner_id=member.id)],
    )

    _, _, recipients, meta = nzp.resolve_network_geo_propagation_recipients(
        net_db,
        admin,
        latitude=47.6062,
        longitude=-122.3321,
        exclude_owner_id=admin.id,
    )
    assert meta["strategy"] == "secondary_zone_creator_only"
    assert recipients == [member.id]


def test_outside_acceptable_zones_delivers_nobody(net_db, monkeypatch):
    network = "NET-3"
    admin = _owner(
        net_db,
        oid=1,
        email="admin@x.com",
        network_id=network,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=47.6062,
        lon=-122.3321,
    )
    admin.account_owner_id = admin.id
    net_db.commit()
    monkeypatch.setattr(
        nzp,
        "evaluate_zone_records_containing_point",
        lambda db, lat, lon: [],
    )

    _, _, recipients, meta = nzp.resolve_network_geo_propagation_recipients(
        net_db,
        admin,
        latitude=40.0,
        longitude=-74.0,
        exclude_owner_id=admin.id,
    )
    assert meta["strategy"] == "network_no_acceptable_zone"
    assert recipients == []


def test_unknown_delivers_global_nearest_regardless_of_zone_id(net_db):
    network = "NET-5"
    sender = _owner(
        net_db,
        oid=1,
        email="sender@x.com",
        network_id=network,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=0.0,
        lon=0.0,
    )
    sender.account_owner_id = sender.id
    _owner(
        net_db,
        oid=2,
        email="near@x.com",
        network_id=network,
        role=OwnerRole.USER,
        account_owner_id=sender.id,
        lat=0.01,
        lon=0.0,
    )
    closer_outsider = _owner(
        net_db,
        oid=99,
        email="outsider@x.com",
        network_id="OTHER-NET",
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=0.005,
        lon=0.0,
    )
    closer_outsider.account_owner_id = closer_outsider.id
    net_db.commit()

    payload = PropagationMessageCreate(
        type=MessageFeatureType.UNKNOWN,
        hid="device-1",
        position=CoordinatePayload(latitude=0.0, longitude=0.0),
        msg={"description": "unknown"},
    )
    result = mfs.create_geo_propagated_message(net_db, sender, payload)
    assert result["fanout"]["strategy"] == "unknown_nearest_global"
    assert result["delivered_owner_ids"][0] == 99
    assert 99 in result["delivered_owner_ids"]
    assert 2 in result["delivered_owner_ids"]


def test_overlapping_primary_and_secondary_networks_all_deliver(net_db, monkeypatch):
    """A point inside network A's primary zone and network B's secondary zone reaches both."""
    net_a = "NET-A"
    net_b = "NET-B"
    admin_a = _owner(
        net_db,
        oid=1,
        email="admin_a@x.com",
        network_id=net_a,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=47.61,
        lon=-122.33,
    )
    admin_a.account_owner_id = admin_a.id
    member_a = _owner(
        net_db,
        oid=2,
        email="member_a@x.com",
        network_id=net_a,
        role=OwnerRole.USER,
        account_owner_id=admin_a.id,
        lat=47.62,
        lon=-122.34,
    )
    admin_b = _owner(
        net_db,
        oid=3,
        email="admin_b@x.com",
        network_id=net_b,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=47.61,
        lon=-122.33,
    )
    admin_b.account_owner_id = admin_b.id
    creator_b = _owner(
        net_db,
        oid=4,
        email="creator_b@x.com",
        network_id=net_b,
        role=OwnerRole.USER,
        account_owner_id=admin_b.id,
        lat=47.61,
        lon=-122.33,
    )
    net_db.commit()

    monkeypatch.setattr(
        nzp,
        "evaluate_zone_records_containing_point",
        lambda db, lat, lon: [401, 402],
    )
    monkeypatch.setattr(
        nzp,
        "zone_ids_for_zone_records",
        lambda db, ids: [net_a, net_b],
    )
    monkeypatch.setattr(
        nzp,
        "_zone_rows_for_records",
        lambda db, ids: [
            # Network A: primary zone (created by its admin).
            _zone_row(record_id=401, network_id=net_a, creator_id=admin_a.id, owner_id=admin_a.id),
            # Network B: secondary zone only (created by a member, not its admin).
            _zone_row(record_id=402, network_id=net_b, creator_id=creator_b.id, owner_id=creator_b.id),
        ],
    )

    _, _, recipients, meta = nzp.resolve_network_geo_propagation_recipients(
        net_db,
        admin_a,
        latitude=47.61,
        longitude=-122.33,
    )
    # Network A (primary) → admin + invited member; Network B (secondary) → creator only.
    assert set(recipients) == {admin_a.id, member_a.id, creator_b.id}
    assert set(meta["matched_network_zone_ids"]) == {net_a, net_b}


def test_overlapping_two_primary_networks_all_deliver(net_db, monkeypatch):
    """A point inside two networks' primary zones reaches both admins + their members."""
    net_a = "NET-PA"
    net_b = "NET-PB"
    admin_a = _owner(
        net_db,
        oid=1,
        email="admin_a@x.com",
        network_id=net_a,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=47.61,
        lon=-122.33,
    )
    admin_a.account_owner_id = admin_a.id
    member_a = _owner(
        net_db,
        oid=2,
        email="member_a@x.com",
        network_id=net_a,
        role=OwnerRole.USER,
        account_owner_id=admin_a.id,
        lat=47.62,
        lon=-122.34,
    )
    admin_b = _owner(
        net_db,
        oid=3,
        email="admin_b@x.com",
        network_id=net_b,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=47.61,
        lon=-122.33,
    )
    admin_b.account_owner_id = admin_b.id
    member_b = _owner(
        net_db,
        oid=4,
        email="member_b@x.com",
        network_id=net_b,
        role=OwnerRole.USER,
        account_owner_id=admin_b.id,
        lat=47.61,
        lon=-122.33,
    )
    net_db.commit()

    monkeypatch.setattr(
        nzp,
        "evaluate_zone_records_containing_point",
        lambda db, lat, lon: [501, 502],
    )
    monkeypatch.setattr(
        nzp,
        "zone_ids_for_zone_records",
        lambda db, ids: [net_a, net_b],
    )
    monkeypatch.setattr(
        nzp,
        "_zone_rows_for_records",
        lambda db, ids: [
            _zone_row(record_id=501, network_id=net_a, creator_id=admin_a.id, owner_id=admin_a.id),
            _zone_row(record_id=502, network_id=net_b, creator_id=admin_b.id, owner_id=admin_b.id),
        ],
    )

    _, _, recipients, meta = nzp.resolve_network_geo_propagation_recipients(
        net_db,
        admin_a,
        latitude=47.61,
        longitude=-122.33,
    )
    assert set(recipients) == {admin_a.id, member_a.id, admin_b.id, member_b.id}
    assert set(meta["matched_network_zone_ids"]) == {net_a, net_b}


def test_owner_participates_in_network_admin_and_invited(net_db):
    network = "NET-PART"
    admin = _owner(
        net_db,
        oid=1,
        email="admin@x.com",
        network_id=network,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=0.0,
        lon=0.0,
    )
    admin.account_owner_id = admin.id
    member = _owner(
        net_db,
        oid=2,
        email="member@x.com",
        network_id=network,
        role=OwnerRole.USER,
        account_owner_id=admin.id,
        lat=0.0,
        lon=0.0,
    )
    outsider = _owner(
        net_db,
        oid=3,
        email="solo@x.com",
        network_id="SOLO-NET",
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=0.0,
        lon=0.0,
    )
    outsider.account_owner_id = outsider.id
    net_db.commit()

    assert nzp.owner_participates_in_network(net_db, admin) is True
    assert nzp.owner_participates_in_network(net_db, member) is True
    assert nzp.owner_participates_in_network(net_db, outsider) is True


def test_owner_not_in_network_without_account_root(net_db):
    network = "NET-LOOSE"
    admin = _owner(
        net_db,
        oid=1,
        email="admin@x.com",
        network_id=network,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=0.0,
        lon=0.0,
    )
    admin.account_owner_id = admin.id
    loose = _owner(
        net_db,
        oid=2,
        email="loose@x.com",
        network_id=network,
        role=OwnerRole.USER,
        account_owner_id=None,
        lat=0.0,
        lon=0.0,
    )
    net_db.commit()

    assert nzp.owner_participates_in_network(net_db, loose) is False


def test_non_invited_user_inside_foreign_primary_zone(net_db, monkeypatch):
    """Tester 1 (solo account) inside Tester 2's primary zone reaches admin + invited member."""
    network = "NET-HOST"
    admin = _owner(
        net_db,
        oid=10,
        email="tester2@x.com",
        network_id=network,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=47.61,
        lon=-122.33,
    )
    admin.account_owner_id = admin.id
    invited = _owner(
        net_db,
        oid=11,
        email="tester3@x.com",
        network_id=network,
        role=OwnerRole.USER,
        account_owner_id=admin.id,
        lat=47.62,
        lon=-122.34,
    )
    outsider = _owner(
        net_db,
        oid=12,
        email="tester1@x.com",
        network_id="NET-TESTER1-SOLO",
        role=OwnerRole.USER,
        account_owner_id=None,
        lat=47.61,
        lon=-122.33,
    )
    net_db.commit()

    monkeypatch.setattr(
        nzp,
        "evaluate_zone_records_containing_point",
        lambda db, lat, lon: [301],
    )
    monkeypatch.setattr(
        nzp,
        "zone_ids_for_zone_records",
        lambda db, ids: [network],
    )
    monkeypatch.setattr(
        nzp,
        "_zone_rows_for_records",
        lambda db, ids: [
            _zone_row(
                record_id=301,
                network_id=network,
                creator_id=admin.id,
                owner_id=admin.id,
            )
        ],
    )

    assert nzp.owner_participates_in_network(net_db, outsider) is False

    _, _, recipients, meta = nzp.resolve_network_geo_propagation_recipients(
        net_db,
        outsider,
        latitude=47.61,
        longitude=-122.33,
        exclude_owner_id=outsider.id,
    )
    assert meta["strategy"] == "primary_zone_network_members"
    assert meta["network_zone_id"] == network
    assert set(recipients) == {admin.id, invited.id}


def _patch_secondary_zone(monkeypatch, *, network: str, admin: Owner, member: Owner, record_id: int = 201):
    monkeypatch.setattr(
        nzp,
        "evaluate_zone_records_containing_point",
        lambda db, lat, lon: [record_id],
    )
    monkeypatch.setattr(
        nzp,
        "zone_ids_for_zone_records",
        lambda db, ids: [network],
    )
    monkeypatch.setattr(
        nzp,
        "_zone_rows_for_records",
        lambda db, ids: [
            _zone_row(record_id=record_id, network_id=network, creator_id=member.id, owner_id=member.id)
        ],
    )


def test_private_plus_secondary_zone_panic_reaches_full_network(net_db, monkeypatch):
    network = "NET-PP"
    admin = _owner(
        net_db,
        oid=1,
        email="pp-admin@x.com",
        network_id=network,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=47.6062,
        lon=-122.3321,
        account_type=AccountType.PRIVATE_PLUS,
    )
    admin.account_owner_id = admin.id
    member = _owner(
        net_db,
        oid=2,
        email="pp-member@x.com",
        network_id=network,
        role=OwnerRole.USER,
        account_owner_id=admin.id,
        lat=47.6070,
        lon=-122.3330,
        account_type=AccountType.PRIVATE_PLUS,
    )
    net_db.commit()
    _patch_secondary_zone(monkeypatch, network=network, admin=admin, member=member)

    payload = PropagationMessageCreate(
        type=MessageFeatureType.PANIC,
        hid="device-pp",
        position=CoordinatePayload(latitude=47.6070, longitude=-122.3330),
        msg={"description": "family panic"},
    )
    result = mfs.create_geo_propagated_message(net_db, member, payload)
    assert result["fanout"]["strategy"] == "private_plus_network_shared"
    assert set(result["delivered_owner_ids"]) == {admin.id}


def test_private_plus_secondary_zone_sensor_still_creator_only(net_db, monkeypatch):
    network = "NET-PP-SENSOR"
    admin = _owner(
        net_db,
        oid=1,
        email="pp-s-admin@x.com",
        network_id=network,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=47.6062,
        lon=-122.3321,
        account_type=AccountType.PRIVATE_PLUS,
    )
    admin.account_owner_id = admin.id
    member = _owner(
        net_db,
        oid=2,
        email="pp-s-member@x.com",
        network_id=network,
        role=OwnerRole.USER,
        account_owner_id=admin.id,
        lat=47.6070,
        lon=-122.3330,
        account_type=AccountType.PRIVATE_PLUS,
    )
    net_db.commit()
    _patch_secondary_zone(monkeypatch, network=network, admin=admin, member=member)

    payload = PropagationMessageCreate(
        type=MessageFeatureType.SENSOR,
        hid="device-pp-s",
        position=CoordinatePayload(latitude=47.6070, longitude=-122.3330),
        msg={"description": "sensor"},
    )
    result = mfs.create_geo_propagated_message(net_db, member, payload)
    assert result["fanout"]["strategy"] == "secondary_zone_creator_only"
    assert result["delivered_owner_ids"] == []


def test_exclusive_secondary_zone_panic_still_creator_only(net_db, monkeypatch):
    network = "NET-EX"
    admin = _owner(
        net_db,
        oid=1,
        email="ex-admin@x.com",
        network_id=network,
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=47.6062,
        lon=-122.3321,
        account_type=AccountType.EXCLUSIVE,
    )
    admin.account_owner_id = admin.id
    member = _owner(
        net_db,
        oid=2,
        email="ex-member@x.com",
        network_id=network,
        role=OwnerRole.USER,
        account_owner_id=admin.id,
        lat=47.6070,
        lon=-122.3330,
        account_type=AccountType.EXCLUSIVE,
    )
    net_db.commit()
    _patch_secondary_zone(monkeypatch, network=network, admin=admin, member=member)

    payload = PropagationMessageCreate(
        type=MessageFeatureType.PANIC,
        hid="device-ex",
        position=CoordinatePayload(latitude=47.6070, longitude=-122.3330),
        msg={"description": "exclusive panic"},
    )
    result = mfs.create_geo_propagated_message(net_db, member, payload)
    assert result["fanout"]["strategy"] == "secondary_zone_creator_only"
    assert result["delivered_owner_ids"] == []
