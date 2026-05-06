"""End-to-end checks: guest QR PERMISSION feed, admin inbox merge, guest thread + CHAT."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app

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


async def _register_admin(client: AsyncClient, zone_id: str) -> tuple[int, str]:
    email = f"e2e-admin-{uuid.uuid4().hex[:10]}@example.com"
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
    admin_id = r.json()["id"]
    lr = await client.post("/owners/login", json={"email": email, "password": "SecurePassword123"})
    assert lr.status_code == 200
    return admin_id, lr.json()["access_token"]


@pytest.mark.asyncio
async def test_permission_request_visible_in_admin_messages_inbox(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"e2e-z-{uuid.uuid4().hex[:6]}"
        admin_id, token = await _register_admin(client, zone_id)
        headers = {"Authorization": f"Bearer {token}"}

        perm = await client.post(
            "/api/access/permission",
            json={"zone_id": zone_id, "guest_name": "Inbox Guest"},
        )
        assert perm.status_code == 200
        guest_id = perm.json()["data"]["guest_id"]

        inbox = await client.get(
            "/messages/",
            params={"owner_id": admin_id, "skip": 0, "limit": 50},
            headers=headers,
        )
        assert inbox.status_code == 200
        rows = inbox.json()
        perm_rows = [r for r in rows if r.get("type") == "PERMISSION"]
        assert perm_rows, "admin inbox should merge ZoneMessageEvent PERMISSION rows"
        assert any(
            "Guest access requested" in str(r.get("message", "")) and "Inbox Guest" in str(r.get("message", ""))
            for r in perm_rows
        ), perm_rows


@pytest.mark.asyncio
async def test_approve_permission_in_admin_and_guest_thread_with_chat(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"e2e-z-{uuid.uuid4().hex[:6]}"
        admin_id, token = await _register_admin(client, zone_id)
        headers = {"Authorization": f"Bearer {token}"}

        perm = await client.post(
            "/api/access/permission",
            json={"zone_id": zone_id, "guest_name": "Thread Guest"},
        )
        assert perm.status_code == 200
        guest_id = perm.json()["data"]["guest_id"]

        ap = await client.post(
            "/api/access/approve",
            headers=headers,
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        assert ap.status_code == 200

        inbox = await client.get(
            "/messages/",
            params={"owner_id": admin_id, "limit": 80},
            headers=headers,
        )
        assert inbox.status_code == 200
        perm_msgs = [r for r in inbox.json() if r.get("type") == "PERMISSION"]
        assert len(perm_msgs) >= 2

        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        code = sess.json()["data"]["exchange_code"]
        gs = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": code},
        )
        assert gs.status_code == 200
        guest_jwt = gs.json()["data"]["access_token"]
        gh = {"Authorization": f"Bearer {guest_jwt}"}

        peers = await client.get(f"/api/guest/zones/{zone_id}/peers", headers=gh)
        assert peers.status_code == 200
        peer_list = peers.json()["data"]["peers"]
        assert peer_list, "guest peers should list zone administrator / host ids"
        assert any(p["owner_id"] == admin_id for p in peer_list)

        dash = await client.get(f"/api/guest/zones/{zone_id}/dashboard", headers=gh)
        assert dash.status_code == 200
        djson = dash.json()["data"]
        assert "map" in djson and isinstance(djson["map"], dict)
        assert "cells" in djson["map"]

        chat = await client.post(
            "/api/guest/messages",
            headers=gh,
            json={
                "zone_id": zone_id,
                "type": "CHAT",
                "text": "Hello from guest workflow e2e",
                "to_owner_id": admin_id,
            },
        )
        assert chat.status_code == 201

        gm = await client.get(
            "/api/guest/messages",
            headers=gh,
            params={"zone_id": zone_id, "with_owner_id": admin_id, "limit": 30},
        )
        assert gm.status_code == 200
        items = gm.json()["data"]["items"]
        types_found = {i["type"] for i in items}
        assert "PERMISSION" in types_found and "CHAT" in types_found
