"""Member-invite QR: never-expire tokens and invited account-type rules."""
from __future__ import annotations

from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.security import create_access_token
from app.database import Base, get_db
from app.main import app
from app.models import Owner
from app.models.owner import AccountType, OwnerRole

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
def override_get_db(test_db, monkeypatch):
    monkeypatch.setattr("app.crud.owner.get_password_hash", lambda password: f"hashed:{password}")

    def _override_get_db():
        yield test_db

    app.dependency_overrides[get_db] = _override_get_db
    yield
    app.dependency_overrides.clear()


def _admin(
    db,
    *,
    email: str,
    zone_id: str,
    account_type: AccountType,
) -> tuple[Owner, str]:
    owner = Owner(
        email=email,
        zone_id=zone_id,
        first_name="Zone",
        last_name="Admin",
        account_type=account_type,
        role=OwnerRole.ADMINISTRATOR,
        hashed_password="x",
        api_key=f"key-{email}",
        address="Admin Address",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(owner)
    db.flush()
    owner.account_owner_id = owner.id
    db.commit()
    db.refresh(owner)
    token = create_access_token({"sub": str(owner.id)})
    return owner, token


async def _join(client: AsyncClient, token: str, email: str, *, zone_id: str | None = None):
    body = {
        "token": token,
        "email": email,
        "first_name": "New",
        "last_name": "Member",
        "password": "SecurePassword123",
        "address": "Member Address",
    }
    if zone_id is not None:
        body["zone_id"] = zone_id
    return await client.post("/utils/qr/join", json=body)


@pytest.mark.asyncio
async def test_qr_generate_never_expires(test_db, override_get_db):
    _, token = _admin(
        test_db,
        email="plus-admin@example.com",
        zone_id="plus-zone",
        account_type=AccountType.PRIVATE_PLUS,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        generate = await client.post(
            "/utils/qr/generate",
            headers={"Authorization": f"Bearer {token}"},
            json={"expires_in_hours": 0},
        )
        assert generate.status_code == 200, generate.text
        body = generate.json()
        assert body["expires_at"] is None
        assert body["token"]


@pytest.mark.asyncio
async def test_qr_join_never_expiring_token(test_db, override_get_db):
    admin, token = _admin(
        test_db,
        email="plus-admin@example.com",
        zone_id="plus-zone",
        account_type=AccountType.PRIVATE_PLUS,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        generate = await client.post(
            "/utils/qr/generate",
            headers={"Authorization": f"Bearer {token}"},
            json={"expires_in_hours": 0},
        )
        assert generate.status_code == 200, generate.text
        join = await _join(client, generate.json()["token"], "joined@example.com")
        assert join.status_code == 200, join.text
        joined = join.json()
        assert joined["zone_id"] == admin.zone_id
        assert joined["account_type"] == "exclusive"
        assert joined["role"] == "user"


@pytest.mark.asyncio
async def test_timed_qr_is_single_use(test_db, override_get_db):
    _, token = _admin(
        test_db,
        email="plus-admin@example.com",
        zone_id="plus-zone",
        account_type=AccountType.PRIVATE_PLUS,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        generate = await client.post(
            "/utils/qr/generate",
            headers={"Authorization": f"Bearer {token}"},
            json={"expires_in_hours": 24},
        )
        assert generate.status_code == 200, generate.text
        invite = generate.json()["token"]
        first = await _join(client, invite, "first@example.com")
        assert first.status_code == 200, first.text
        second = await _join(client, invite, "second@example.com")
        assert second.status_code == 400
        body = second.json()
        text = str(body.get("message") or body.get("detail") or "").lower()
        assert "already used" in text


@pytest.mark.asyncio
async def test_infinity_qr_is_multi_use(test_db, override_get_db):
    admin, token = _admin(
        test_db,
        email="plus-admin@example.com",
        zone_id="plus-zone",
        account_type=AccountType.PRIVATE_PLUS,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        generate = await client.post(
            "/utils/qr/generate",
            headers={"Authorization": f"Bearer {token}"},
            json={"expires_in_hours": 0},
        )
        assert generate.status_code == 200, generate.text
        invite = generate.json()["token"]
        first = await _join(client, invite, "first@example.com")
        assert first.status_code == 200, first.text
        second = await _join(client, invite, "second@example.com")
        assert second.status_code == 200, second.text
        assert second.json()["account_type"] == "exclusive"
        assert second.json()["account_owner_id"] == admin.id
        assert second.json()["email"] != first.json()["email"]


@pytest.mark.asyncio
async def test_qr_generate_rejected_for_exclusive_admin(test_db, override_get_db):
    """Exclusive accounts are solo and cannot generate member-invite QR codes."""
    _, token = _admin(
        test_db,
        email="exclusive-admin@example.com",
        zone_id="exclusive-zone",
        account_type=AccountType.EXCLUSIVE,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        generate = await client.post(
            "/utils/qr/generate",
            headers={"Authorization": f"Bearer {token}"},
            json={"expires_in_hours": 24},
        )
        assert generate.status_code == 403, generate.text
        body = generate.json()
        text = str(body.get("message") or body.get("detail") or "").lower()
        assert "member invite" in text or "solo" in text or "exclusive" in text


@pytest.mark.asyncio
async def test_qr_join_system_admin_provisions_individual_user(test_db, override_get_db):
    """Private (system admin) invites create Individual user accounts for a new network."""
    admin, token = _admin(
        test_db,
        email="admin@test.com",
        zone_id="DISTRICT-11",
        account_type=AccountType.PRIVATE,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        generate = await client.post(
            "/utils/qr/generate",
            headers={"Authorization": f"Bearer {token}"},
            json={"expires_in_hours": 0},
        )
        assert generate.status_code == 200, generate.text
        assert generate.json()["expires_at"] is None
        invite = generate.json()["token"]

        preview = await client.get("/utils/qr/preview", params={"token": invite})
        assert preview.status_code == 200, preview.text
        assert preview.json()["invite_kind"] == "new_network_admin"
        assert preview.json()["account_type"] == "exclusive"
        assert preview.json()["zone_id"] is None

        missing_zone = await _join(client, invite, "invited-admin@example.com")
        assert missing_zone.status_code == 422

        join = await _join(
            client,
            invite,
            "invited-admin@example.com",
            zone_id="NEW-NETWORK-42",
        )
        assert join.status_code == 200, join.text
        joined = join.json()
        assert joined["account_type"] == "exclusive"
        assert joined["role"] == "user"
        assert joined["zone_id"] == "NEW-NETWORK-42"
        assert joined["zone_id"] != admin.zone_id
        assert joined["account_owner_id"] == joined["id"]


@pytest.mark.asyncio
async def test_qr_join_system_admin_rejects_duplicate_network_id(test_db, override_get_db):
    admin, token = _admin(
        test_db,
        email="admin@test.com",
        zone_id="DISTRICT-11",
        account_type=AccountType.PRIVATE,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        generate = await client.post(
            "/utils/qr/generate",
            headers={"Authorization": f"Bearer {token}"},
            json={"expires_in_hours": 24},
        )
        assert generate.status_code == 200, generate.text
        join = await _join(
            client,
            generate.json()["token"],
            "dup-network@example.com",
            zone_id=admin.zone_id,
        )
        assert join.status_code == 409
        text = str(join.json().get("message") or join.json().get("detail") or "").lower()
        assert "network id" in text or "already" in text


@pytest.mark.asyncio
async def test_qr_preview_member_invite(test_db, override_get_db):
    admin, token = _admin(
        test_db,
        email="plus-admin@example.com",
        zone_id="plus-zone",
        account_type=AccountType.PRIVATE_PLUS,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        generate = await client.post(
            "/utils/qr/generate",
            headers={"Authorization": f"Bearer {token}"},
            json={"expires_in_hours": 24},
        )
        assert generate.status_code == 200, generate.text
        preview = await client.get(
            "/utils/qr/preview",
            params={"token": generate.json()["token"]},
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["invite_kind"] == "member"
        assert body["zone_id"] == admin.zone_id
        assert body["account_type"] == "exclusive"
