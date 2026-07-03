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
) -> Owner:
    owner = Owner(
        id=oid,
        email=email,
        zone_id=network_id,
        first_name="T",
        last_name=str(oid),
        account_type=AccountType.PRIVATE,
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


def test_unknown_scoped_to_network_only(net_db):
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
    outsider = _owner(
        net_db,
        oid=99,
        email="outsider@x.com",
        network_id="OTHER-NET",
        role=OwnerRole.ADMINISTRATOR,
        account_owner_id=None,
        lat=0.005,
        lon=0.0,
    )
    outsider.account_owner_id = outsider.id
    net_db.commit()

    payload = PropagationMessageCreate(
        type=MessageFeatureType.UNKNOWN,
        hid="device-1",
        position=CoordinatePayload(latitude=0.0, longitude=0.0),
        msg={"description": "unknown"},
    )
    result = mfs.create_geo_propagated_message(net_db, sender, payload)
    assert result["fanout"]["strategy"] == "unknown_nearest_network"
    assert result["delivered_owner_ids"] == [2]
    assert 99 not in result["delivered_owner_ids"]


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
