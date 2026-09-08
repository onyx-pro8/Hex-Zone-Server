import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.database import Base, get_db
from app.main import app
from app.services.zone_policy import (
    build_capabilities,
    member_secondary_limit_for_primary_count,
    normalize_zone_name,
)


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
    original_admin = settings.MAX_ZONES_ADMINISTRATOR
    original_primary = settings.MAX_ZONES_ADMINISTRATOR_PRIMARY
    original_user = settings.MAX_ZONES_USER
    settings.MAX_ZONES_ADMINISTRATOR = 3
    settings.MAX_ZONES_ADMINISTRATOR_PRIMARY = 2
    settings.MAX_ZONES_USER = 1
    yield
    settings.MAX_ZONES_ADMINISTRATOR = original_admin
    settings.MAX_ZONES_ADMINISTRATOR_PRIMARY = original_primary
    settings.MAX_ZONES_USER = original_user


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _register_and_login(
    client: AsyncClient,
    email: str,
    role: str,
    zone_id: str,
    account_owner_id: int | None = None,
):
    payload = {
        "email": email,
        "zone_id": zone_id,
        "first_name": "Test",
        "last_name": "User",
        "account_type": "private_plus",
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

    login = await client.post(
        "/owners/login", json={"email": email, "password": "SecurePassword123"}
    )
    assert login.status_code == 200, login.text
    return owner_id, login.json()["access_token"]


def _zone_payload(name: str) -> dict:
    return {
        "name": name,
        "type": "custom_1",
        "geometry": {},
        "config": {"communal_id": "COMM-1"},
    }


def test_member_secondary_limit_tracks_admin_primary_count(policy_limits):
    assert member_secondary_limit_for_primary_count(1) == 2
    assert member_secondary_limit_for_primary_count(2) == 1
    assert member_secondary_limit_for_primary_count(0) == 3


def test_build_capabilities_admin_allows_third_as_secondary(policy_limits):
    caps = build_capabilities(
        "administrator", total_zones=2, admin_primary_count=2
    )
    assert caps.can_create_zone is True
    assert caps.remaining_total == 1
    assert caps.max_total == 3
    assert caps.next_zone_is_primary is False


def test_build_capabilities_admin_blocks_at_three(policy_limits):
    caps = build_capabilities(
        "administrator", total_zones=3, admin_primary_count=2
    )
    assert caps.can_create_zone is False
    assert caps.remaining_total == 0
    assert "3 zones" in (caps.reason or "")


def test_build_capabilities_member_depends_on_primary_count(policy_limits):
    with_one_primary = build_capabilities(
        "user", total_zones=0, admin_primary_count=1
    )
    assert with_one_primary.max_total == 2
    assert with_one_primary.can_create_zone is True

    with_two_primary = build_capabilities(
        "user", total_zones=1, admin_primary_count=2
    )
    assert with_two_primary.max_total == 1
    assert with_two_primary.can_create_zone is False
    assert with_two_primary.reason == "Maximum of 1 secondary zone for members reached."


def test_normalize_zone_name_trims_and_validates():
    assert normalize_zone_name("  Alpha Zone  ") == "Alpha Zone"
    with pytest.raises(Exception):
        normalize_zone_name("   ")


@pytest.mark.asyncio
async def test_admin_third_zone_is_secondary(zone_test_db, policy_limits):
    async with _client() as client:
        _, admin_token = await _register_and_login(
            client, "admin-tier@example.com", "administrator", "tier-shared"
        )
        headers = {"Authorization": f"Bearer {admin_token}"}

        first = await client.post("/zones/", headers=headers, json=_zone_payload("Zone A"))
        second = await client.post("/zones/", headers=headers, json=_zone_payload("Zone B"))
        third = await client.post("/zones/", headers=headers, json=_zone_payload("Zone C"))
        fourth = await client.post("/zones/", headers=headers, json=_zone_payload("Zone D"))

        assert first.status_code == 201
        assert first.json()["is_primary"] is True
        assert second.status_code == 201
        assert second.json()["is_primary"] is True
        assert third.status_code == 201
        assert third.json()["is_primary"] is False
        assert fourth.status_code == 409
        assert fourth.json()["error_code"] == "ZONE_QUOTA_MAX_TOTAL_REACHED"


@pytest.mark.asyncio
async def test_delete_frees_create_slot_and_name(zone_test_db, policy_limits):
    async with _client() as client:
        _, admin_token = await _register_and_login(
            client, "admin-reuse@example.com", "administrator", "reuse-shared"
        )
        headers = {"Authorization": f"Bearer {admin_token}"}

        created = []
        for name in ("Zone A", "Zone B", "Zone C"):
            response = await client.post(
                "/zones/", headers=headers, json=_zone_payload(name)
            )
            assert response.status_code == 201, response.text
            created.append(response.json())

        blocked = await client.post(
            "/zones/", headers=headers, json=_zone_payload("Zone D")
        )
        assert blocked.status_code == 409
        assert blocked.json()["error_code"] == "ZONE_QUOTA_MAX_TOTAL_REACHED"

        deleted = await client.delete(f"/zones/{created[2]['id']}", headers=headers)
        assert deleted.status_code == 204

        caps = await client.get("/zones/capabilities", headers=headers)
        assert caps.status_code == 200
        assert caps.json()["can_create_zone"] is True
        assert caps.json()["remaining_total"] == 1

        # Soft-deleted name may be reused once the slot is free.
        recreated = await client.post(
            "/zones/", headers=headers, json=_zone_payload("Zone C")
        )
        assert recreated.status_code == 201, recreated.text
        assert recreated.json()["name"] == "Zone C"


@pytest.mark.asyncio
async def test_member_secondary_quota_and_eviction(zone_test_db, policy_limits):
    async with _client() as client:
        admin_id, admin_token = await _register_and_login(
            client, "admin-evict@example.com", "administrator", "evict-shared"
        )
        user_id, user_token = await _register_and_login(
            client,
            "user-evict@example.com",
            "user",
            "evict-shared",
            account_owner_id=admin_id,
        )
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        user_headers = {"Authorization": f"Bearer {user_token}"}

        primary = await client.post(
            "/zones/", headers=admin_headers, json=_zone_payload("Admin Primary")
        )
        assert primary.status_code == 201
        assert primary.json()["is_primary"] is True

        first_secondary = await client.post(
            "/zones/", headers=user_headers, json=_zone_payload("User One")
        )
        second_secondary = await client.post(
            "/zones/", headers=user_headers, json=_zone_payload("User Two")
        )
        assert first_secondary.status_code == 201
        assert first_secondary.json()["is_primary"] is False
        assert second_secondary.status_code == 201
        overflow = await client.post(
            "/zones/", headers=user_headers, json=_zone_payload("User Three")
        )
        assert overflow.status_code == 409

        # Member can see own secondaries + admin primary; admin cannot see member secondaries.
        user_list = await client.get("/zones/", headers=user_headers)
        admin_list = await client.get("/zones/", headers=admin_headers)
        assert user_list.status_code == 200
        assert {row["name"] for row in user_list.json()} == {
            "Admin Primary",
            "User One",
            "User Two",
        }
        assert admin_list.status_code == 200
        assert {row["name"] for row in admin_list.json()} == {"Admin Primary"}

        # Creating a second admin primary evicts the member's latest secondary.
        second_primary = await client.post(
            "/zones/", headers=admin_headers, json=_zone_payload("Admin Primary 2")
        )
        assert second_primary.status_code == 201
        assert second_primary.json()["is_primary"] is True
        evicted = second_primary.json().get("evicted_zones") or []
        assert len(evicted) == 1
        assert evicted[0]["name"] == "User Two"
        assert int(evicted[0]["creator_id"]) == int(user_id)

        user_list_after = await client.get("/zones/", headers=user_headers)
        names_after = {row["name"] for row in user_list_after.json()}
        assert "User Two" not in names_after
        assert "User One" in names_after
        assert "Admin Primary 2" in names_after

        # With 2 primaries, member may keep only 1 secondary.
        user_caps = await client.get("/zones/capabilities", headers=user_headers)
        assert user_caps.status_code == 200
        assert user_caps.json()["can_create_zone"] is False
        assert user_caps.json()["max_total"] == 1


@pytest.mark.asyncio
async def test_update_auth_primary_admin_only(zone_test_db, policy_limits):
    async with _client() as client:
        admin_id, admin_token = await _register_and_login(
            client, "admin-edit@example.com", "administrator", "edit-shared"
        )
        _, user_token = await _register_and_login(
            client,
            "user-edit@example.com",
            "user",
            "edit-shared",
            account_owner_id=admin_id,
        )
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        user_headers = {"Authorization": f"Bearer {user_token}"}

        created = await client.post(
            "/zones/", headers=admin_headers, json=_zone_payload("Admin Editable")
        )
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
async def test_user_cannot_delete_admin_primary_zone(zone_test_db, policy_limits):
    async with _client() as client:
        admin_id, admin_token = await _register_and_login(
            client, "admin-ndel@example.com", "administrator", "ndel-shared"
        )
        _, user_token = await _register_and_login(
            client,
            "user-ndel@example.com",
            "user",
            "ndel-shared",
            account_owner_id=admin_id,
        )
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        user_headers = {"Authorization": f"Bearer {user_token}"}

        created = await client.post(
            "/zones/", headers=admin_headers, json=_zone_payload("Admin Zone")
        )
        assert created.status_code == 201
        zone_record_id = created.json()["id"]

        forbidden = await client.delete(f"/zones/{zone_record_id}", headers=user_headers)
        assert forbidden.status_code == 403
        assert forbidden.json()["error_code"] == "ZONE_DELETE_FORBIDDEN"


@pytest.mark.asyncio
async def test_admin_cannot_delete_member_secondary(zone_test_db, policy_limits):
    async with _client() as client:
        admin_id, admin_token = await _register_and_login(
            client, "admin-sdel@example.com", "administrator", "sdel-shared"
        )
        _, user_token = await _register_and_login(
            client,
            "user-sdel@example.com",
            "user",
            "sdel-shared",
            account_owner_id=admin_id,
        )
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        user_headers = {"Authorization": f"Bearer {user_token}"}

        await client.post(
            "/zones/", headers=admin_headers, json=_zone_payload("Admin Zone")
        )
        created = await client.post(
            "/zones/", headers=user_headers, json=_zone_payload("Member Zone")
        )
        assert created.status_code == 201
        zone_record_id = created.json()["id"]

        forbidden = await client.delete(f"/zones/{zone_record_id}", headers=admin_headers)
        assert forbidden.status_code == 403
        assert forbidden.json()["error_code"] == "ZONE_DELETE_FORBIDDEN"


@pytest.mark.asyncio
async def test_concurrent_create_at_boundary_allows_single_success(zone_test_db, policy_limits):
    original_admin = settings.MAX_ZONES_ADMINISTRATOR
    settings.MAX_ZONES_ADMINISTRATOR = 1
    settings.MAX_ZONES_ADMINISTRATOR_PRIMARY = 1
    try:
        async with _client() as client:
            _, admin_token = await _register_and_login(
                client, "admin-race@example.com", "administrator", "race-shared"
            )
            headers = {"Authorization": f"Bearer {admin_token}"}

            async def create_zone(index: int):
                return await client.post(
                    "/zones/", headers=headers, json=_zone_payload(f"Race {index}")
                )

            first, second = await asyncio.gather(create_zone(1), create_zone(2))
            codes = sorted([first.status_code, second.status_code])
            assert codes == [201, 409]
    finally:
        settings.MAX_ZONES_ADMINISTRATOR = original_admin
        settings.MAX_ZONES_ADMINISTRATOR_PRIMARY = 2
