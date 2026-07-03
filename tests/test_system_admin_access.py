"""System administrator visibility across zones and members."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.services.access_policy import (
    messaging_visible_owner_ids,
    visible_owner_ids,
    zone_listing_owner_ids,
)
from app.services.member_service import list_members
from app.services.zone_policy import ensure_zone_edit_allowed


class _ZoneStub:
    def __init__(self, *, owner_id: int, creator_id: int) -> None:
        self.owner_id = owner_id
        self.creator_id = creator_id


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


def _owner(
    db,
    *,
    email: str,
    zone_id: str,
    account_type: AccountType,
    role: OwnerRole,
    account_owner_id: int | None = None,
) -> Owner:
    owner = Owner(
        email=email,
        zone_id=zone_id,
        first_name="Test",
        last_name="User",
        account_type=account_type,
        role=role,
        account_owner_id=account_owner_id,
        hashed_password="x",
        api_key=f"key-{email}",
        address="addr",
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


def test_system_admin_sees_all_owners_zones_and_members(db):
    system_admin = _owner(
        db,
        email="admin@test.com",
        zone_id="DISTRICT-11",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
    )
    other_admin = _owner(
        db,
        email="other-admin@example.com",
        zone_id="NET-OTHER",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    other_member = _owner(
        db,
        email="member@example.com",
        zone_id="NET-OTHER",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.USER,
        account_owner_id=other_admin.id,
    )
    db.commit()

    all_ids = {system_admin.id, other_admin.id, other_member.id}
    assert set(visible_owner_ids(db, system_admin, include_inactive=True)) == all_ids
    assert set(zone_listing_owner_ids(db, system_admin)) == all_ids
    assert set(messaging_visible_owner_ids(db, system_admin, include_inactive=True)) == all_ids

    members = list_members(db, system_admin)
    assert {int(row["id"]) for row in members} == all_ids


def test_account_admin_still_limited_to_own_account(db):
    system_admin = _owner(
        db,
        email="admin@test.com",
        zone_id="DISTRICT-11",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
    )
    other_admin = _owner(
        db,
        email="other-admin@example.com",
        zone_id="NET-OTHER",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    other_member = _owner(
        db,
        email="member@example.com",
        zone_id="NET-OTHER",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.USER,
        account_owner_id=other_admin.id,
    )
    db.commit()

    visible = set(visible_owner_ids(db, other_admin, include_inactive=True))
    assert other_admin.id in visible
    assert other_member.id in visible
    assert system_admin.id not in visible


def test_system_admin_may_edit_any_zone(db):
    system_admin = _owner(
        db,
        email="admin@test.com",
        zone_id="DISTRICT-11",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
    )
    other_admin = _owner(
        db,
        email="other-admin@example.com",
        zone_id="NET-OTHER",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    zone = _ZoneStub(owner_id=other_admin.id, creator_id=other_admin.id)
    db.commit()

    ensure_zone_edit_allowed(system_admin, zone)  # type: ignore[arg-type]
