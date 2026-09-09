"""Individual accounts receive a unique server-assigned Communal ID."""
from __future__ import annotations

from datetime import datetime

import pytest
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
from fastapi import HTTPException


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


def test_assign_owner_communal_id_for_individual(db):
    owner = _owner(db, email="solo@example.com", account_type=AccountType.EXCLUSIVE)
    cid = assign_owner_communal_id(db, owner)
    assert cid.startswith("COMM-")
    assert owner.communal_id == cid
    # Idempotent
    assert assign_owner_communal_id(db, owner) == cid


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


def test_individual_locked_to_assigned_id(db):
    owner = _owner(db, email="solo@example.com", account_type=AccountType.EXCLUSIVE)
    assigned = assign_owner_communal_id(db, owner)
    assert resolve_communal_id_for_owner(owner, None) == assigned
    assert resolve_communal_id_for_owner(owner, assigned) == assigned
    with pytest.raises(HTTPException) as exc:
        resolve_communal_id_for_owner(owner, "COMM-OTHER")
    assert exc.value.status_code == 403


def test_invited_individuals_get_unique_ids(db):
    first = _owner(db, email="a@example.com", account_type=AccountType.EXCLUSIVE)
    second = _owner(db, email="b@example.com", account_type=AccountType.EXCLUSIVE)
    a = assign_owner_communal_id(db, first)
    b = assign_owner_communal_id(db, second)
    assert a != b
