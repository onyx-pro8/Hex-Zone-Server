"""Individuals/members no longer receive Communal IDs; admins mint public ones."""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.services.communal_zone_service import (
    assert_may_generate_communal_id,
    assign_owner_communal_id,
    resolve_communal_id_for_owner,
)


TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture
def db():
    engine = create_engine(TEST_DATABASE_URL, echo=False)
    testing_session_maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    with testing_session_maker() as session:
        yield session
    Base.metadata.drop_all(bind=engine)


def _owner(db, *, email: str, account_type: AccountType, role: OwnerRole = OwnerRole.USER):
    owner = Owner(
        email=email,
        zone_id="zone-1",
        first_name="Test",
        last_name="User",
        account_type=account_type,
        role=role,
        hashed_password="x",
        api_key=f"key-{email}",
        address="Addr",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(owner)
    db.flush()
    owner.account_owner_id = owner.id
    db.commit()
    db.refresh(owner)
    return owner


def test_assign_owner_communal_id_no_longer_mints(db):
    owner = _owner(db, email="solo@example.com", account_type=AccountType.EXCLUSIVE)
    assert assign_owner_communal_id(db, owner) == ""
    assert owner.communal_id is None


def test_assign_skips_non_individual(db):
    owner = _owner(
        db,
        email="family@example.com",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    assert assign_owner_communal_id(db, owner) == ""
    assert owner.communal_id is None


def test_individual_cannot_generate(db):
    owner = _owner(db, email="solo@example.com", account_type=AccountType.EXCLUSIVE)
    with pytest.raises(HTTPException) as exc:
        assert_may_generate_communal_id(owner)
    assert exc.value.status_code == 403


def test_member_cannot_generate(db):
    owner = _owner(
        db,
        email="member@example.com",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    with pytest.raises(HTTPException) as exc:
        assert_may_generate_communal_id(owner)
    assert exc.value.status_code == 403


def test_admin_may_generate(db):
    owner = _owner(
        db,
        email="admin@example.com",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    assert_may_generate_communal_id(owner)


def test_individual_cannot_attach_communal_ids(db):
    owner = _owner(db, email="solo@example.com", account_type=AccountType.EXCLUSIVE)
    with pytest.raises(HTTPException) as exc:
        resolve_communal_id_for_owner(owner, "COMM-ABC")
    assert exc.value.status_code == 403


def test_admin_may_resolve_requested_id(db):
    owner = _owner(
        db,
        email="admin@example.com",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    assert resolve_communal_id_for_owner(owner, "comm-abc") == "COMM-ABC"
