"""User signup links to an administrator by Network ID, not a numeric owner id."""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.owner import AccountType, Owner, OwnerRole
from app.services.access_policy import (
    AMBIGUOUS_NETWORK_ADMIN_DETAIL,
    NETWORK_ADMIN_EMAIL_NOT_FOUND_DETAIL,
    NETWORK_ADMIN_NOT_FOUND_DETAIL,
    list_joinable_networks,
    resolve_account_owner_id,
)


SQLALCHEMY_DATABASE_URL = "sqlite://"


@pytest.fixture()
def test_db():
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def override_get_db(test_db):
    def _override_get_db():
        try:
            yield test_db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    yield
    app.dependency_overrides.clear()


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _admin(
    db,
    *,
    email: str,
    zone_id: str,
    account_type: AccountType = AccountType.PRIVATE_PLUS,
) -> Owner:
    owner = Owner(
        email=email,
        zone_id=zone_id,
        first_name="Net",
        last_name="Admin",
        account_type=account_type,
        role=OwnerRole.ADMINISTRATOR,
        hashed_password="x",
        api_key=_unique("key"),
        address="1 Admin St",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(owner)
    db.flush()
    owner.account_owner_id = owner.id
    db.flush()
    return owner


def _resolve(db, *, zone_id: str, account_type: str = "private_plus", email: str | None = None):
    return resolve_account_owner_id(
        db,
        role="user",
        requested_account_owner_id=None,
        zone_id=zone_id,
        account_type=account_type,
        administrator_email=email,
    )


def test_joinable_network_list_omits_system_admins(test_db):
    _admin(test_db, email="ada@example.com", zone_id="Family-Net")
    _admin(
        test_db,
        email="root@example.com",
        zone_id="Platform",
        account_type=AccountType.PRIVATE,
    )
    rows = list_joinable_networks(test_db)
    ids = [row["network_id"] for row in rows]
    assert ids == ["Family-Net"]
    assert rows[0]["label"] == "Family"
    assert rows[0]["administrator_count"] == 1


def test_network_id_links_the_matching_administrator(test_db):
    admin = _admin(test_db, email="ada@example.com", zone_id="Family-Net")
    assert _resolve(test_db, zone_id="family-net") == admin.id


def test_unknown_network_id_is_rejected(test_db):
    _admin(test_db, email="ada@example.com", zone_id="Family-Net")
    with pytest.raises(HTTPException) as exc:
        _resolve(test_db, zone_id="Other-Net")
    assert exc.value.status_code == 422
    assert exc.value.detail == NETWORK_ADMIN_NOT_FOUND_DETAIL


def test_joinable_networks_match_the_selected_account_type(test_db):
    _admin(test_db, email="ada@example.com", zone_id="Family-Net")
    _admin(
        test_db,
        email="pro@example.com",
        zone_id="Pro-Net",
        account_type=AccountType.ENHANCED,
    )
    family = list_joinable_networks(test_db, account_type="PRIVATE_PLUS")
    pro = list_joinable_networks(test_db, account_type="enhanced")
    assert [row["network_id"] for row in family] == ["Family-Net"]
    assert [row["network_id"] for row in pro] == ["Pro-Net"]


def test_different_account_type_does_not_join_the_network(test_db):
    _admin(
        test_db,
        email="ada@example.com",
        zone_id="Family-Net",
        account_type=AccountType.ENHANCED,
    )
    with pytest.raises(HTTPException) as exc:
        _resolve(test_db, zone_id="Family-Net", account_type="private_plus")
    assert exc.value.detail == NETWORK_ADMIN_NOT_FOUND_DETAIL


def test_system_administrator_network_is_not_joinable(test_db):
    _admin(
        test_db,
        email="root@example.com",
        zone_id="Platform",
        account_type=AccountType.PRIVATE,
    )
    with pytest.raises(HTTPException) as exc:
        _resolve(test_db, zone_id="Platform")
    assert exc.value.detail == NETWORK_ADMIN_NOT_FOUND_DETAIL


def test_shared_network_requires_administrator_email(test_db):
    _admin(test_db, email="ada@example.com", zone_id="Shared-Net")
    _admin(test_db, email="bea@example.com", zone_id="shared-net")
    with pytest.raises(HTTPException) as exc:
        _resolve(test_db, zone_id="Shared-Net")
    assert exc.value.detail == AMBIGUOUS_NETWORK_ADMIN_DETAIL


def test_administrator_email_picks_one_of_several(test_db):
    first = _admin(test_db, email="ada@example.com", zone_id="Shared-Net")
    second = _admin(test_db, email="bea@example.com", zone_id="Shared-Net")
    assert _resolve(test_db, zone_id="Shared-Net", email="Bea@example.com") == second.id
    assert _resolve(test_db, zone_id="Shared-Net", email="ada@example.com") == first.id


def test_unknown_administrator_email_is_rejected(test_db):
    _admin(test_db, email="ada@example.com", zone_id="Family-Net")
    with pytest.raises(HTTPException) as exc:
        _resolve(test_db, zone_id="Family-Net", email="missing@example.com")
    assert exc.value.detail == NETWORK_ADMIN_EMAIL_NOT_FOUND_DETAIL


def test_contract_join_inherits_the_administrator_plan(test_db, monkeypatch):
    monkeypatch.setattr("app.services.auth_service.get_password_hash", lambda _password: "hashed")
    monkeypatch.setattr(
        "app.services.auth_service.sync_owner_home_from_address",
        lambda _owner: None,
    )
    from app.services.auth_service import register_user

    admin = _admin(
        test_db,
        email="ada@example.com",
        zone_id="Pro-Net",
        account_type=AccountType.ENHANCED,
    )
    created = register_user(
        test_db,
        {
            "name": "Mia Member",
            "email": "mia-join@example.com",
            "password": "SecurePassword123",
            "accountType": "ENHANCED",
            "registrationType": "USER",
            "zoneId": "pro-net",
            "address": "2 Member St",
        },
    )
    assert created["account_owner_id"] == admin.id
    assert created["account_type"] == "exclusive"


def test_explicit_account_owner_id_still_links(test_db):
    admin = _admin(test_db, email="ada@example.com", zone_id="Family-Net")
    resolved = resolve_account_owner_id(
        test_db,
        role="user",
        requested_account_owner_id=admin.id,
        zone_id="ignored",
        account_type="private_plus",
    )
    assert resolved == admin.id


@pytest.mark.asyncio
async def test_register_endpoints_link_user_by_network_id(test_db, override_get_db, monkeypatch):
    monkeypatch.setattr(
        "app.services.auth_service.sync_owner_home_from_address",
        lambda owner: None,
    )
    monkeypatch.setattr(
        "app.services.auth_service.get_password_hash",
        lambda password: "hashed",
    )
    monkeypatch.setattr(
        "app.crud.owner.get_password_hash",
        lambda password: "hashed",
    )
    network = _unique("family-net")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await client.post(
            "/owners/register",
            json={
                "email": _unique("admin") + "@example.com",
                "zone_id": network,
                "first_name": "Ada",
                "last_name": "Admin",
                "account_type": "private_plus",
                "role": "administrator",
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "1 Admin St",
            },
        )
        assert admin.status_code == 201, admin.text
        admin_id = admin.json()["id"]

        legacy = await client.post(
            "/owners/register",
            json={
                "email": _unique("member") + "@example.com",
                "zone_id": network.upper(),
                "first_name": "Mia",
                "last_name": "Member",
                "account_type": "private_plus",
                "role": "user",
                "password": "SecurePassword123",
                "address": "2 Member St",
            },
        )
        assert legacy.status_code == 201, legacy.text
        assert legacy.json()["account_owner_id"] == admin_id

        contract = await client.post(
            "/register",
            json={
                "name": "Ned Member",
                "email": _unique("contract") + "@example.com",
                "password": "SecurePassword123",
                "accountType": "PRIVATE_PLUS",
                "registrationType": "USER",
                "zoneId": network,
                "address": "3 Member St",
            },
        )
        assert contract.status_code == 201, contract.text
        body = contract.json()
        payload = body.get("data", body)
        assert payload["account_owner_id"] == admin_id
