import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.database import Base, get_db
from app.main import app
from app.services.zone_policy import build_capabilities, normalize_zone_name


@pytest.fixture
def zone_test_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def _override_get_db():
        db = testing_session_maker()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    yield
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def policy_limits():
    original_max = settings.MAX_ZONES_TOTAL
    original_reserved = settings.RESERVED_FOR_STANDARD_USERS
    settings.MAX_ZONES_TOTAL = 3
    settings.RESERVED_FOR_STANDARD_USERS = 1
    yield
    settings.MAX_ZONES_TOTAL = original_max
    settings.RESERVED_FOR_STANDARD_USERS = original_reserved


async def _register_and_login(client: AsyncClient, email: str, role: str, zone_id: str, account_owner_id: int | None = None):
    payload = {
        "email": email,
        "zone_id": zone_id,
        "first_name": "Test",
        "last_name": "User",
        "account_type": "private",
        "role": role,
        "password": "SecurePassword123",
        "address": "Address",
    }
    if role == "administrator":
        payload["registration_code"] = "FREE"
    if account_owner_id is not None:
        payload["account_owner_id"] = account_owner_id
    register = await client.post("/owners/register", json=payload)
    assert register.status_code == 201, register.text
    owner_id = register.json()["id"]

    login = await client.post("/owners/login", json={"email": email, "password": "SecurePassword123"})
    assert login.status_code == 200, login.text
    return owner_id, login.json()["access_token"]


def _zone_payload(name: str) -> dict:
    return {
        "name": name,
        "type": "custom_1",
        "geometry": {},
        "config": {"communal_id": "COMM-1"},
    }


def test_build_capabilities_admin_respects_reserved_slot(policy_limits):
    caps = build_capabilities("administrator", total_zones=2)
    assert caps.can_create_zone is False
    assert caps.remaining_total == 1
    assert caps.remaining_for_role == 0
    assert caps.reason == "A standard-user slot must remain available."


def test_normalize_zone_name_trims_and_validates():
    assert normalize_zone_name("  Alpha Zone  ") == "Alpha Zone"
    with pytest.raises(Exception):
        normalize_zone_name("   ")


@pytest.mark.asyncio
async def test_admin_cannot_consume_reserved_standard_slot(zone_test_db, policy_limits):
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, admin_token = await _register_and_login(client, "admin-quota@example.com", "administrator", "quota-shared")
        headers = {"Authorization": f"Bearer {admin_token}"}

        first = await client.post("/zones/", headers=headers, json=_zone_payload("  Zone A  "))
        second = await client.post("/zones/", headers=headers, json=_zone_payload("Zone B"))
        third = await client.post("/zones/", headers=headers, json=_zone_payload("Zone C"))

        assert first.status_code == 201
        assert second.status_code == 201
        assert third.status_code == 409
        assert third.json()["error_code"] == "ZONE_QUOTA_RESERVED_FOR_STANDARD"


@pytest.mark.asyncio
async def test_standard_user_can_create_reserved_slot_and_capabilities_reflect_state(zone_test_db, policy_limits):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_id, admin_token = await _register_and_login(client, "admin-cap@example.com", "administrator", "caps-shared")
        _, user_token = await _register_and_login(
            client,
            "user-cap@example.com",
            "user",
            "caps-shared",
            account_owner_id=admin_id,
        )
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        user_headers = {"Authorization": f"Bearer {user_token}"}

        await client.post("/zones/", headers=admin_headers, json=_zone_payload("Admin One"))
        await client.post("/zones/", headers=admin_headers, json=_zone_payload("Admin Two"))

        user_create = await client.post("/zones/", headers=user_headers, json=_zone_payload("  User One  "))
        assert user_create.status_code == 201
        assert user_create.json()["name"] == "User One"

        caps = await client.get("/zones/capabilities", headers=admin_headers)
        assert caps.status_code == 200
        payload = caps.json()
        assert payload["role"] == "administrator"
        assert payload["can_create_zone"] is False
        assert payload["reason"] is not None


@pytest.mark.asyncio
async def test_update_auth_and_normalized_name(zone_test_db, policy_limits):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_id, admin_token = await _register_and_login(client, "admin-edit@example.com", "administrator", "edit-shared")
        _, user_token = await _register_and_login(
            client,
            "user-edit@example.com",
            "user",
            "edit-shared",
            account_owner_id=admin_id,
        )
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        user_headers = {"Authorization": f"Bearer {user_token}"}

        created = await client.post("/zones/", headers=admin_headers, json=_zone_payload("Admin Editable"))
        zone_record_id = created.json()["id"]

        forbidden = await client.patch(
            f"/zones/{zone_record_id}",
            headers=user_headers,
            json={"name": "Try Edit"},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["error_code"] == "ZONE_EDIT_FORBIDDEN"

        updated = await client.patch(
            f"/zones/{zone_record_id}",
            headers=admin_headers,
            json={"name": "  Renamed Zone  "},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "Renamed Zone"


@pytest.mark.asyncio
async def test_owner_can_delete_zone_by_record_id(zone_test_db, policy_limits):
    async with AsyncClient(app=app, base_url="http://test") as client:
        _, admin_token = await _register_and_login(client, "admin-del@example.com", "administrator", "del-shared")
        headers = {"Authorization": f"Bearer {admin_token}"}

        created = await client.post("/zones/", headers=headers, json=_zone_payload("Delete Me"))
        assert created.status_code == 201
        zone_record_id = created.json()["id"]

        deleted = await client.delete(f"/zones/{zone_record_id}", headers=headers)
        assert deleted.status_code == 204

        missing = await client.get(f"/zones/{zone_record_id}", headers=headers)
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_admin_can_delete_member_zone(zone_test_db, policy_limits):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_id, admin_token = await _register_and_login(client, "admin-mdel@example.com", "administrator", "mdel-shared")
        _, user_token = await _register_and_login(
            client,
            "user-mdel@example.com",
            "user",
            "mdel-shared",
            account_owner_id=admin_id,
        )
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        user_headers = {"Authorization": f"Bearer {user_token}"}

        created = await client.post("/zones/", headers=user_headers, json=_zone_payload("Member Zone"))
        assert created.status_code == 201
        zone_record_id = created.json()["id"]

        deleted = await client.delete(f"/zones/{zone_record_id}", headers=admin_headers)
        assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_user_cannot_delete_admin_zone(zone_test_db, policy_limits):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_id, admin_token = await _register_and_login(client, "admin-ndel@example.com", "administrator", "ndel-shared")
        _, user_token = await _register_and_login(
            client,
            "user-ndel@example.com",
            "user",
            "ndel-shared",
            account_owner_id=admin_id,
        )
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        user_headers = {"Authorization": f"Bearer {user_token}"}

        created = await client.post("/zones/", headers=admin_headers, json=_zone_payload("Admin Zone"))
        assert created.status_code == 201
        zone_record_id = created.json()["id"]

        forbidden = await client.delete(f"/zones/{zone_record_id}", headers=user_headers)
        assert forbidden.status_code == 403
        assert forbidden.json()["error_code"] == "ZONE_DELETE_FORBIDDEN"


@pytest.mark.asyncio
async def test_concurrent_create_at_boundary_allows_single_success(zone_test_db, policy_limits):
    original_max = settings.MAX_ZONES_TOTAL
    original_reserved = settings.RESERVED_FOR_STANDARD_USERS
    settings.MAX_ZONES_TOTAL = 2
    settings.RESERVED_FOR_STANDARD_USERS = 1
    try:
        async with AsyncClient(app=app, base_url="http://test") as client:
            _, admin_token = await _register_and_login(client, "admin-race@example.com", "administrator", "race-shared")
            headers = {"Authorization": f"Bearer {admin_token}"}

            async def create_zone(index: int):
                return await client.post("/zones/", headers=headers, json=_zone_payload(f"Race {index}"))

            first, second = await asyncio.gather(create_zone(1), create_zone(2))
            codes = sorted([first.status_code, second.status_code])
            assert codes == [201, 409]
    finally:
        settings.MAX_ZONES_TOTAL = original_max
        settings.RESERVED_FOR_STANDARD_USERS = original_reserved
