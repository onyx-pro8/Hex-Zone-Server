"""Universal access requests open other networks' primary zones in that place."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.services import universal_access_service

PHOTO = "data:image/png;base64,iVBORw0KGgo="


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


def test_approved_code_shows_other_network_primary_zone(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.ENHANCED_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    member = _owner(
        db,
        email="member@example.com",
        zone_id="NET-A",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
        account_owner_id=admin.id,
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
    db.execute(
        text(
            """
            INSERT INTO zones (
                zone_id, owner_id, creator_id, zone_type, name, h3_cells,
                parameters, active, is_primary, created_at, updated_at
            ) VALUES (
                'other-zone', :owner_id, :owner_id, 'GEOFENCE', 'City hall', '[]',
                :parameters, 1, 1, :now, :now
            )
            """
        ),
        {
            "owner_id": other.id,
            "parameters": (
                '{"config": {"city": "Austin", "region": "Texas", "country": "United States"},'
                ' "geometry": {"geo_fence_polygon": {"type": "Polygon",'
                ' "coordinates": [[[-97.8, 30.2], [-97.7, 30.2], [-97.7, 30.3], [-97.8, 30.2]]]}}}'
            ),
            "now": datetime.utcnow().isoformat(sep=" "),
        },
    )
    db.commit()
    zone_id = db.execute(text("SELECT id FROM zones WHERE zone_id = 'other-zone'")).scalar()

    created = universal_access_service.create_request(
        db,
        admin=admin,
        member_id=member.id,
        address="9 Congress Ave",
        photo_id=PHOTO,
        country="United States",
        region="Texas",
        city="Austin",
    )
    assert created["status"] == "pending"
    assert created["email"] == member.email

    from app.services.guest_api_service import list_zone_permission_events_for_owner_feed

    system_feed = list_zone_permission_events_for_owner_feed(db, owner=system_admin)
    assert any("Universal access code requested" in row.text for row in system_feed)
    admin_feed = list_zone_permission_events_for_owner_feed(db, owner=admin)
    assert any("Universal access code requested" in row.text for row in admin_feed)
    member_feed = list_zone_permission_events_for_owner_feed(db, owner=member)
    assert all("Universal access code requested" not in row.text for row in member_feed)

    approved = universal_access_service.approve_request(
        db,
        caller=system_admin,
        request_id=created["id"],
        resolve_boundary=False,
    )
    assert approved["code"]
    assert approved["enabled"] is True

    visible = universal_access_service.universal_primary_zones(db, member, already_ids=set())
    assert [row.id for row in visible] == [zone_id]

    universal_access_service.set_credential_enabled(
        db,
        caller=admin,
        credential_id=approved["credential_id"],
        enabled=False,
    )
    assert universal_access_service.universal_primary_zones(db, member, already_ids=set()) == []
