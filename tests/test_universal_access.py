"""Universal access requests share selected primary zones with an invitee."""
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


def _insert_primary_zone(db, *, owner_id: int, zone_key: str, name: str) -> int:
    db.execute(
        text(
            """
            INSERT INTO zones (
                zone_id, owner_id, creator_id, zone_type, name, h3_cells,
                parameters, active, is_primary, created_at, updated_at
            ) VALUES (
                :zone_key, :owner_id, :owner_id, 'GEOFENCE', :name, '[]',
                '{}', 1, 1, :now, :now
            )
            """
        ),
        {
            "zone_key": zone_key,
            "owner_id": owner_id,
            "name": name,
            "now": datetime.utcnow().isoformat(sep=" "),
        },
    )
    db.flush()
    return int(db.execute(text("SELECT id FROM zones WHERE zone_id = :zid"), {"zid": zone_key}).scalar())


def test_approved_code_shows_selected_primary_zones(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.ENHANCED_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    invitee = _owner(
        db,
        email="worker@example.com",
        zone_id="NET-B",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
        account_owner_id=None,
    )
    invitee.account_owner_id = invitee.id
    system_admin = _owner(
        db,
        email="sys@example.com",
        zone_id="SYS",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
    )
    shared_a = _insert_primary_zone(db, owner_id=admin.id, zone_key="zone-a", name="City hall")
    shared_b = _insert_primary_zone(db, owner_id=admin.id, zone_key="zone-b", name="Depot")
    _insert_primary_zone(db, owner_id=admin.id, zone_key="zone-c", name="Hidden")
    db.commit()

    created = universal_access_service.create_request(
        db,
        admin=admin,
        invitee_email=invitee.email,
        zone_ids=[shared_a, shared_b],
        photo_id=PHOTO,
        business_document=BUSINESS,
    )
    assert created["status"] == "pending"
    assert created["email"] == invitee.email
    assert created["zone_ids"] == [shared_a, shared_b]

    from app.services.guest_api_service import list_zone_permission_events_for_owner_feed

    system_feed = list_zone_permission_events_for_owner_feed(db, owner=system_admin)
    assert any("Universal access code requested" in row.text for row in system_feed)

    approved = universal_access_service.approve_request(
        db,
        caller=system_admin,
        request_id=created["id"],
    )
    assert approved["code"]
    assert approved["enabled"] is True
    assert approved["zone_ids"] == [shared_a, shared_b]

    grant_feed_admin = list_zone_permission_events_for_owner_feed(db, owner=admin)
    grant_feed_invitee = list_zone_permission_events_for_owner_feed(db, owner=invitee)
    assert any("Universal access approved" in row.text for row in grant_feed_admin)
    assert any("Universal access approved" in row.text for row in grant_feed_invitee)

    visible = universal_access_service.universal_primary_zones(db, invitee, already_ids=set())
    assert [row.id for row in visible] == [shared_a, shared_b]

    universal_access_service.set_credential_enabled(
        db,
        caller=admin,
        credential_id=approved["credential_id"],
        enabled=False,
    )
    assert universal_access_service.universal_primary_zones(db, invitee, already_ids=set()) == []


def test_family_admin_cannot_request_universal_access(db):
    admin = _owner(
        db,
        email="family@example.com",
        zone_id="NET-F",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    invitee = _owner(
        db,
        email="guest@example.com",
        zone_id="NET-G",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    invitee.account_owner_id = invitee.id
    zone_id = _insert_primary_zone(db, owner_id=admin.id, zone_key="zone-f", name="Home")
    db.commit()

    with pytest.raises(Exception) as raised:
        universal_access_service.create_request(
            db,
            admin=admin,
            invitee_email=invitee.email,
            zone_ids=[zone_id],
            photo_id=PHOTO,
            business_document=BUSINESS,
        )
    assert "Individual Pro and Organization" in str(raised.value)
