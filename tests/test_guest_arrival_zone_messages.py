"""Admin guest arrival copy overrides and guest-facing snapshot behavior."""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.main import app
from app.database import Base, get_db
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import AccessSchedule, GuestAccessSession
from app.models.guest_access_zone_message import GuestAccessZoneMessage
from app.services.guest_arrival_zone_messages import (
    DEFAULT_EXPECTED_ARRIVAL_MESSAGE,
    DEFAULT_UNEXPECTED_ARRIVAL_MESSAGE,
    MAX_GUEST_ARRIVAL_MESSAGE_LEN,
)

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
    email = f"ga-msg-admin-{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post(
        "/owners/register",
        json={
            "email": email,
            "zone_id": zone_id,
            "first_name": "Admin",
            "last_name": "User",
            "account_type": "private",
            "password": "SecurePassword123",
            "registration_code": "FREE",
            "address": "Addr",
        },
    )
    assert r.status_code == 201, r.text
    reg = r.json()
    owner_id = reg["id"]
    lr = await client.post("/owners/login", json={"email": email, "password": "SecurePassword123"})
    assert lr.status_code == 200
    return owner_id, lr.json()["access_token"]


@pytest.mark.asyncio
async def test_defaults_when_no_zone_message_row(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = "zone-ga-msg-default"
        await _register_admin(client, zone_id=zone_id)

        perm = await client.post(
            "/api/access/permission",
            json={"zone_id": zone_id, "guest_name": "Walk In"},
        )
        assert perm.status_code == 200
        assert perm.json()["data"]["message"] == DEFAULT_UNEXPECTED_ARRIVAL_MESSAGE

        guest_id = perm.json()["data"]["guest_id"]
        poll = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        assert poll.status_code == 200
        assert poll.json()["data"]["message"] == DEFAULT_UNEXPECTED_ARRIVAL_MESSAGE


@pytest.mark.asyncio
async def test_admin_get_put_custom_messages_permission_and_poll(test_db, override_get_db):
    from datetime import datetime, timedelta

    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = "zone-ga-msg-custom"
        _, token = await _register_admin(client, zone_id=zone_id)

        g = await client.get(
            f"/api/access/zones/{zone_id}/guest-arrival-messages",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert g.status_code == 200
        gj = g.json()["data"]
        assert gj["zone_id"] == zone_id
        assert gj["expected_arrival_message"] is None
        assert gj["defaults"]["expected_arrival_message"] == DEFAULT_EXPECTED_ARRIVAL_MESSAGE

        custom_unexp = "Please wait at reception."
        custom_exp = "Welcome — proceed to lobby."
        p = await client.patch(
            f"/api/access/zones/{zone_id}/guest-arrival-messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"unexpected_arrival_message": custom_unexp, "expected_arrival_message": custom_exp},
        )
        assert p.status_code == 200
        assert p.json()["data"]["unexpected_arrival_message"] == custom_unexp

        perm = await client.post(
            "/api/access/permission",
            json={"zone_id": zone_id, "guest_name": "No Sched"},
        )
        assert perm.status_code == 200
        assert perm.json()["data"]["message"] == custom_unexp

        guest_id = perm.json()["data"]["guest_id"]
        poll = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        assert poll.json()["data"]["message"] == custom_unexp

        sched = AccessSchedule(
            zone_id=zone_id,
            guest_name="Sched Guest",
            event_id=None,
            starts_at=datetime.utcnow() - timedelta(hours=1),
            ends_at=datetime.utcnow() + timedelta(hours=1),
            active=True,
            notify_member_assist=False,
        )
        test_db.add(sched)
        test_db.commit()

        perm2 = await client.post(
            "/api/access/permission",
            json={"zone_id": zone_id, "guest_name": "Sched Guest"},
        )
        assert perm2.status_code == 200
        assert perm2.json()["data"]["message"] == custom_exp
        gid2 = perm2.json()["data"]["guest_id"]
        poll2 = await client.get(f"/api/access/session/{gid2}", params={"zone_id": zone_id})
        assert poll2.json()["data"]["message"] == custom_exp


@pytest.mark.asyncio
async def test_snapshot_unchanged_after_admin_updates_messages(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = "zone-ga-msg-snap"
        _, token = await _register_admin(client, zone_id=zone_id)

        await client.patch(
            f"/api/access/zones/{zone_id}/guest-arrival-messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"unexpected_arrival_message": "Version A"},
        )
        perm = await client.post(
            "/api/access/permission",
            json={"zone_id": zone_id, "guest_name": "Waiter"},
        )
        guest_id = perm.json()["data"]["guest_id"]
        assert perm.json()["data"]["message"] == "Version A"

        await client.patch(
            f"/api/access/zones/{zone_id}/guest-arrival-messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"unexpected_arrival_message": "Version B"},
        )
        poll = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        assert poll.json()["data"]["message"] == "Version A"


@pytest.mark.asyncio
async def test_guest_arrival_messages_unauthorized_and_forbidden(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_a = "zone-ga-msg-a"
        zone_b = "zone-ga-msg-b"
        await _register_admin(client, zone_id=zone_a)
        _, token_b = await _register_admin(client, zone_id=zone_b)

        r = await client.get(f"/api/access/zones/{zone_a}/guest-arrival-messages")
        assert r.status_code == 401

        r2 = await client.get(
            f"/api/access/zones/{zone_a}/guest-arrival-messages",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert r2.status_code == 403


@pytest.mark.asyncio
async def test_guest_arrival_messages_trim_and_max_length(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = "zone-ga-msg-val"
        _, token = await _register_admin(client, zone_id=zone_id)

        bad = await client.patch(
            f"/api/access/zones/{zone_id}/guest-arrival-messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"unexpected_arrival_message": "   "},
        )
        assert bad.status_code == 422

        too_long = "x" * (MAX_GUEST_ARRIVAL_MESSAGE_LEN + 1)
        bad2 = await client.patch(
            f"/api/access/zones/{zone_id}/guest-arrival-messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"unexpected_arrival_message": too_long},
        )
        assert bad2.status_code == 422

        ok = await client.patch(
            f"/api/access/zones/{zone_id}/guest-arrival-messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"unexpected_arrival_message": "  hello  "},
        )
        assert ok.status_code == 200
        assert ok.json()["data"]["unexpected_arrival_message"] == "hello"


@pytest.mark.asyncio
async def test_legacy_session_without_snapshot_uses_live_template(test_db, override_get_db):
    """Pre-migration rows: null snapshot + unexpected pending → poll uses current zone template."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = "zone-ga-msg-legacy"
        _, token = await _register_admin(client, zone_id=zone_id)

        row = GuestAccessSession(
            guest_id=str(uuid.uuid4()),
            zone_id=zone_id,
            guest_name="Legacy",
            event_id=None,
            device_id=None,
            latitude=None,
            longitude=None,
            kind="unexpected",
            resolution="pending",
            schedule_id=None,
            admin_owner_id=None,
            qr_token_id=None,
            arrival_guest_message_snapshot=None,
        )
        test_db.add(row)
        test_db.commit()

        test_db.add(
            GuestAccessZoneMessage(
                zone_id=zone_id,
                unexpected_arrival_message="Live template",
                expected_arrival_message=None,
                guest_pass_verified_message=None,
            )
        )
        test_db.commit()

        poll = await client.get(f"/api/access/session/{row.guest_id}", params={"zone_id": zone_id})
        assert poll.status_code == 200
        assert poll.json()["data"]["message"] == "Live template"

        await client.patch(
            f"/api/access/zones/{zone_id}/guest-arrival-messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"unexpected_arrival_message": DEFAULT_UNEXPECTED_ARRIVAL_MESSAGE},
        )
        poll2 = await client.get(f"/api/access/session/{row.guest_id}", params={"zone_id": zone_id})
        assert poll2.json()["data"]["message"] == DEFAULT_UNEXPECTED_ARRIVAL_MESSAGE
