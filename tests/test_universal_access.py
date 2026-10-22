"""Universal access invite and place-access requests."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.services import universal_access_service

PHOTO = "data:image/png;base64,iVBORw0KGgo="
BUSINESS = "data:application/pdf;base64,JVBERi0="


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


def _owner(db, *, email, zone_id, account_type, role, account_owner_id=None):
    owner = Owner(
        email=email,
        zone_id=zone_id,
        first_name="Ada",
        last_name="Lovelace",
        account_type=account_type,
        role=role,
        account_owner_id=account_owner_id,
        hashed_password="x",
        api_key=f"key-{email}",
        address="1 Main",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(owner)
    db.flush()
    if account_owner_id is None and role == OwnerRole.ADMINISTRATOR:
        owner.account_owner_id = owner.id
    db.flush()
    return owner


def _insert_zone(
    db,
    *,
    owner_id: int,
    zone_key: str,
    name: str,
    is_primary: bool = True,
    parameters: str = "{}",
) -> int:
    db.execute(
        text(
            """
            INSERT INTO zones (
                zone_id, owner_id, creator_id, zone_type, name, h3_cells,
                parameters, active, is_primary, created_at, updated_at
            ) VALUES (
                :zone_key, :owner_id, :owner_id, 'GEOFENCE', :name, '[]',
                :parameters, 1, :is_primary, :now, :now
            )
            """
        ),
        {
            "zone_key": zone_key,
            "owner_id": owner_id,
            "name": name,
            "parameters": parameters,
            "is_primary": 1 if is_primary else 0,
            "now": datetime.utcnow().isoformat(sep=" "),
        },
    )
    db.flush()
    return int(db.execute(text("SELECT id FROM zones WHERE zone_id = :zid"), {"zid": zone_key}).scalar())


def test_invite_request_shares_selected_zones(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    invitee = _owner(
        db,
        email="worker@example.com",
        zone_id="NET-B",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    invitee.account_owner_id = invitee.id
    system_admin = _owner(
        db,
        email="sys@example.com",
        zone_id="SYS",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
    )
    shared_a = _insert_zone(db, owner_id=admin.id, zone_key="zone-a", name="City hall")
    shared_b = _insert_zone(db, owner_id=admin.id, zone_key="zone-b", name="Depot")
    _insert_zone(db, owner_id=admin.id, zone_key="zone-c", name="Secondary only", is_primary=False)
    db.commit()

    created = universal_access_service.create_invite_request(
        db,
        admin=admin,
        invitee_email=invitee.email,
        zone_ids=[shared_a, shared_b],
    )
    assert created["request_kind"] == "invite"
    assert created["status"] == "pending"

    approved = universal_access_service.approve_request(
        db,
        caller=system_admin,
        request_id=created["id"],
    )
    assert approved["enabled"] is True
    visible = universal_access_service.universal_primary_zones(db, invitee, already_ids=set())
    assert [row.id for row in visible] == [shared_a, shared_b]


def test_place_access_lets_system_admin_pick_matching_zones(db, monkeypatch):
    monkeypatch.setattr(universal_access_service, "_reverse_place", lambda lng, lat: {})
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.ENHANCED,
        role=OwnerRole.ADMINISTRATOR,
    )
    other = _owner(
        db,
        email="other@example.com",
        zone_id="NET-B",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    system_admin = _owner(
        db,
        email="sys@example.com",
        zone_id="SYS",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
    )
    austin = _insert_zone(
        db,
        owner_id=other.id,
        zone_key="austin-hall",
        name="Austin hall",
        parameters='{"config": {"city": "Austin", "region": "Texas", "country": "United States"}}',
    )
    _insert_zone(
        db,
        owner_id=other.id,
        zone_key="dallas-hall",
        name="Dallas hall",
        parameters='{"config": {"city": "Dallas", "region": "Texas", "country": "United States"}}',
    )
    db.commit()

    created = universal_access_service.create_place_access_request(
        db,
        admin=admin,
        country="United States",
        region="Texas",
        city="Austin",
        photo_id=PHOTO,
        business_document=BUSINESS,
    )
    assert created["request_kind"] == "place_access"
    assert any(zone["id"] == austin for zone in created["candidate_zones"])

    approved = universal_access_service.approve_request(
        db,
        caller=system_admin,
        request_id=created["id"],
        zone_ids=[austin],
    )
    assert approved["zone_ids"] == [austin]
    visible = universal_access_service.universal_primary_zones(db, admin, already_ids=set())
    assert [row.id for row in visible] == [austin]


def test_place_access_finds_mapped_zones_in_other_networks(db, monkeypatch):
    def fake_reverse(lng, lat):
        if lng < -40:
            return {
                "country": "United States",
                "region": "New York",
                "city": "Watervliet",
                "country_code": "us",
            }
        return {"country": "France", "region": "Ile-de-France", "city": "Paris", "country_code": "fr"}

    monkeypatch.setattr(universal_access_service, "_reverse_place", fake_reverse)
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.ENHANCED,
        role=OwnerRole.ADMINISTRATOR,
    )
    other = _owner(
        db,
        email="other@example.com",
        zone_id="NET-OTHER",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    us_polygon = (
        '{"geometry":{"geo_fence_polygon":{"type":"Polygon","coordinates":'
        '[[[-73.8,42.7],[-73.6,42.7],[-73.6,42.8],[-73.8,42.7]]]}}}'
    )
    france_polygon = (
        '{"geometry":{"geo_fence_polygon":{"type":"Polygon","coordinates":'
        '[[[2.3,48.8],[2.4,48.8],[2.4,48.9],[2.3,48.8]]]}}}'
    )
    us_zone = _insert_zone(
        db,
        owner_id=other.id,
        zone_key="ny-primary",
        name="My zone",
        parameters=us_polygon,
    )
    _insert_zone(
        db,
        owner_id=other.id,
        zone_key="ny-secondary",
        name="Secondary lot",
        is_primary=False,
        parameters=us_polygon,
    )
    _insert_zone(
        db,
        owner_id=other.id,
        zone_key="paris-primary",
        name="Paris hall",
        parameters=france_polygon,
    )
    db.commit()

    created = universal_access_service.create_place_access_request(
        db,
        admin=admin,
        country="United States",
        region="",
        city="",
        photo_id=PHOTO,
        business_document=BUSINESS,
    )
    assert [zone["id"] for zone in created["candidate_zones"]] == [us_zone]
    assert created["candidate_zones"][0]["owner_id"] == other.id


def test_invitee_search_includes_other_networks(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.ENHANCED,
        role=OwnerRole.ADMINISTRATOR,
    )
    _owner(
        db,
        email="worker@city.gov",
        zone_id="NET-OTHER",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    db.commit()

    matches = universal_access_service.search_invitees(db, admin=admin, query="city.gov")
    assert [row["email"] for row in matches] == ["worker@city.gov"]
    assert matches[0]["network_id"] == "NET-OTHER"
