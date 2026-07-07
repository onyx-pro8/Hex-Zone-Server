"""System administrator account-type management."""
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.crud import owner as owner_crud
from app.database import Base
from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.schemas.schemas import AccountTypeEnum, OwnerUpdate
from app.services.account_type_policy import assert_account_type_change_allowed


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


def test_cannot_demote_last_system_administrator(db):
    system_admin = _owner(
        db,
        email="admin@test.com",
        zone_id="DISTRICT-11",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
    )
    db.commit()

    with pytest.raises(HTTPException) as exc:
        assert_account_type_change_allowed(
            db,
            system_admin,
            system_admin,
            AccountType.PRIVATE_PLUS.value,
        )
    assert exc.value.status_code == 403


def test_system_admin_can_promote_administrator_to_private(db):
    system_admin = _owner(
        db,
        email="admin@test.com",
        zone_id="DISTRICT-11",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
    )
    target = _owner(
        db,
        email="promote-me@example.com",
        zone_id="promote-zone",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    member = _owner(
        db,
        email="member@example.com",
        zone_id="promote-zone",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.USER,
        account_owner_id=target.id,
    )
    db.commit()

    assert_account_type_change_allowed(
        db,
        system_admin,
        target,
        AccountType.PRIVATE.value,
    )
    updated = owner_crud.update_owner(
        db,
        target.id,
        OwnerUpdate(account_type=AccountTypeEnum.PRIVATE),
    )
    db.commit()
    db.refresh(member)

    assert updated is not None
    assert updated.account_type == AccountType.PRIVATE
    assert member.account_type == AccountType.PRIVATE


def test_non_system_admin_cannot_change_account_type(db):
    regular_admin = _owner(
        db,
        email="regular-admin@example.com",
        zone_id="regular-zone",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    user = _owner(
        db,
        email="regular-user@example.com",
        zone_id="regular-zone",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.USER,
        account_owner_id=regular_admin.id,
    )
    db.commit()

    with pytest.raises(HTTPException) as exc:
        assert_account_type_change_allowed(
            db,
            regular_admin,
            user,
            AccountType.EXCLUSIVE.value,
        )
    assert exc.value.status_code == 403


def test_private_account_type_requires_administrator_role(db):
    system_admin = _owner(
        db,
        email="admin@test.com",
        zone_id="DISTRICT-11",
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
    )
    user = _owner(
        db,
        email="user@example.com",
        zone_id="DISTRICT-11",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
        account_owner_id=system_admin.id,
    )
    db.commit()

    with pytest.raises(HTTPException) as exc:
        assert_account_type_change_allowed(
            db,
            system_admin,
            user,
            AccountType.PRIVATE.value,
        )
    assert exc.value.status_code == 422
