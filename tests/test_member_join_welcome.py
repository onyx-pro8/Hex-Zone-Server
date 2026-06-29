"""Welcome SERVICE message when a new user joins a zone account."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import Base, get_db
from app.main import app
from app.services.member_join_welcome_service import DEFAULT_MEMBER_JOIN_WELCOME, render_member_join_welcome
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture
def test_db():
    engine = create_engine(TEST_DATABASE_URL, echo=False)
    testing_session_maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    with testing_session_maker() as session:
        yield session
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def override_get_db(test_db):
    def _override_get_db():
        yield test_db

    app.dependency_overrides[get_db] = _override_get_db
    yield
    app.dependency_overrides.clear()


async def _register_admin(client: AsyncClient, *, zone_id: str) -> tuple[int, str]:
    email = f"mjw-admin-{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post(
        "/owners/register",
        json={
            "email": email,
            "zone_id": zone_id,
            "first_name": "Zone",
            "last_name": "Admin",
            "account_type": "private",
            "password": "SecurePassword123",
            "registration_code": "FREE",
            "address": "Admin Address",
        },
    )
    assert response.status_code == 201, response.text
    owner_id = response.json()["id"]
    login = await client.post("/owners/login", json={"email": email, "password": "SecurePassword123"})
    assert login.status_code == 200
    return owner_id, login.json()["access_token"]


@pytest.mark.asyncio
async def test_qr_join_notifies_existing_members(test_db, override_get_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        zone_id = f"zone-mjw-{uuid.uuid4().hex[:8]}"
        admin_id, admin_token = await _register_admin(client, zone_id=zone_id)

        generate = await client.post(
            "/utils/qr/generate",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"expires_in_hours": 24},
        )
        assert generate.status_code == 200
        token = generate.json()["token"]

        join = await client.post(
            "/utils/qr/join",
            json={
                "token": token,
                "email": f"mjw-user-{uuid.uuid4().hex[:8]}@example.com",
                "first_name": "New",
                "last_name": "Member",
                "password": "SecurePassword123",
                "address": "Member Address",
            },
        )
        assert join.status_code == 200
        joined_id = join.json()["id"]

        admin_messages = await client.get(
            f"/messages/?owner_id={admin_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert admin_messages.status_code == 200
        expected = "Welcome! New Member has joined the zone."
        admin_texts = [row["message"] for row in admin_messages.json()]
        assert expected in admin_texts
        assert any(row.get("type") == "SERVICE" for row in admin_messages.json())

        joined_login = await client.post(
            "/owners/login",
            json={"email": join.json()["email"], "password": "SecurePassword123"},
        )
        assert joined_login.status_code == 200
        joined_token = joined_login.json()["access_token"]

        joined_messages = await client.get(
            f"/messages/?owner_id={joined_id}",
            headers={"Authorization": f"Bearer {joined_token}"},
        )
        assert joined_messages.status_code == 200
        joined_texts = [row["message"] for row in joined_messages.json()]
        assert expected not in joined_texts


@pytest.mark.asyncio
async def test_user_register_notifies_admin(test_db, override_get_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        zone_id = f"zone-mjw-reg-{uuid.uuid4().hex[:8]}"
        admin_id, admin_token = await _register_admin(client, zone_id=zone_id)

        user_email = f"mjw-reg-user-{uuid.uuid4().hex[:8]}@example.com"
        register = await client.post(
            "/owners/register",
            json={
                "email": user_email,
                "zone_id": zone_id,
                "first_name": "Registered",
                "last_name": "User",
                "account_type": "private",
                "role": "user",
                "account_owner_id": admin_id,
                "password": "SecurePassword123",
                "address": "User Address",
            },
        )
        assert register.status_code == 201, register.text

        admin_messages = await client.get(
            f"/messages/?owner_id={admin_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert admin_messages.status_code == 200
        expected = "Welcome! Registered User has joined the zone."
        assert expected in [row["message"] for row in admin_messages.json()]


def test_render_member_join_welcome_default():
    class Stub:
        first_name = "Ada"
        last_name = "Lovelace"

    assert render_member_join_welcome(Stub()) == DEFAULT_MEMBER_JOIN_WELCOME.replace(
        "{member_name}", "Ada Lovelace"
    )
