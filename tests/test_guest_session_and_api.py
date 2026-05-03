"""Approved-guest exchange, JWT, and guest-only messaging API."""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.main import app
from app.database import Base, get_db
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
    email = f"guest-admin-{uuid.uuid4().hex[:10]}@example.com"
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
    body = lr.json()
    return owner_id, body["access_token"]


@pytest.mark.asyncio
async def test_guest_exchange_consumed_and_guest_apis(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_id, admin_token = await _register_admin(client, zone_id="zone-guest-1")

        perm = await client.post(
            "/api/access/permission",
            json={"zone_id": "zone-guest-1", "guest_name": "Walk-in Pat"},
        )
        assert perm.status_code == 200
        guest_id = perm.json()["guest_id"]
        zone_id = perm.json()["zone_id"]

        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        assert sess.status_code == 200
        assert sess.json()["status"] == "UNEXPECTED"
        assert "exchange_code" not in sess.json()

        ap = await client.post(
            "/api/access/approve",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        assert ap.status_code == 200

        sess2 = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        assert sess2.status_code == 200
        body = sess2.json()
        assert body["status"] == "APPROVED"
        assert "exchange_code" in body and body["exchange_code"]
        assert "exchange_expires_at" in body and body["exchange_expires_at"]
        code = body["exchange_code"]

        gs = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": code},
        )
        assert gs.status_code == 200
        gdata = gs.json()["data"]
        assert gdata["token_type"] == "Bearer"
        assert gdata["expires_in"] >= 60
        assert gdata["guest"]["guest_id"] == guest_id
        guest_jwt = gdata["access_token"]

        gs2 = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": code},
        )
        assert gs2.status_code == 409
        assert gs2.json().get("error_code") == "exchange_consumed"

        me = await client.get("/api/guest/me", headers={"Authorization": f"Bearer {guest_jwt}"})
        assert me.status_code == 200
        assert me.json()["data"]["guest_id"] == guest_id

        bad = await client.get("/api/guest/me", headers={"Authorization": "Bearer not-a-jwt"})
        assert bad.status_code == 401

        peers = await client.get(
            f"/api/guest/zones/{zone_id}/peers",
            headers={"Authorization": f"Bearer {guest_jwt}"},
        )
        assert peers.status_code == 200
        plist = peers.json()["data"]["peers"]
        assert any(p["owner_id"] == admin_id for p in plist)

        bad_msg = await client.post(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            json={
                "zone_id": zone_id,
                "type": "SERVICE",
                "text": "nope",
                "to_owner_id": admin_id,
            },
        )
        assert bad_msg.status_code == 403
        assert bad_msg.json().get("error_code") == "forbidden_message_type"

        chat = await client.post(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            json={
                "zone_id": zone_id,
                "type": "CHAT",
                "text": "Hello admin",
                "to_owner_id": admin_id,
            },
        )
        assert chat.status_code == 201
        ev = chat.json()["data"]
        assert ev["type"] == "CHAT"
        assert ev["from"]["kind"] == "guest"
        assert ev["to"]["owner_id"] == admin_id

        lst = await client.get(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            params={"zone_id": zone_id, "with_owner_id": admin_id, "limit": 20},
        )
        assert lst.status_code == 200
        items = lst.json()["data"]["items"]
        assert any(i["text"] == "Hello admin" for i in items)


@pytest.mark.asyncio
async def test_guest_chat_visible_to_recipient_row(test_db, override_get_db):
    """Recipient should see guest-sent CHAT in zone_message_events (propagation persistence)."""
    from app.models.zone_message_event import ZoneMessageEvent

    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_id, admin_token = await _register_admin(client, zone_id="zone-guest-2")

        perm = await client.post(
            "/api/access/permission",
            json={"zone_id": "zone-guest-2", "guest_name": "Visitor"},
        )
        guest_id = perm.json()["guest_id"]
        zone_id = perm.json()["zone_id"]
        await client.post(
            "/api/access/approve",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        code = sess.json()["exchange_code"]
        gs = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": code},
        )
        guest_jwt = gs.json()["data"]["access_token"]

        await client.post(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            json={"zone_id": zone_id, "type": "CHAT", "text": "Ping", "to_owner_id": admin_id},
        )

        row = (
            test_db.query(ZoneMessageEvent)
            .filter(
                ZoneMessageEvent.zone_id == zone_id,
                ZoneMessageEvent.sender_guest_id == guest_id,
                ZoneMessageEvent.receiver_id == admin_id,
            )
            .first()
        )
        assert row is not None
        assert row.type == "CHAT"
        assert "Ping" in row.text


@pytest.mark.asyncio
async def test_member_list_guest_requests_access_api(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"zone-gr-{uuid.uuid4().hex[:8]}"
        admin_id, admin_token = await _register_admin(client, zone_id=zone_id)

        perm = await client.post(
            "/api/access/permission",
            json={"zone_id": zone_id, "guest_name": "List Me"},
        )
        assert perm.status_code == 200
        guest_id = perm.json()["guest_id"]

        no_auth = await client.get("/api/access/guest-requests", params={"zone_id": zone_id})
        assert no_auth.status_code == 401

        bad_zone = await client.get(
            "/api/access/guest-requests",
            params={"zone_id": "nonexistent-zone-xyz"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert bad_zone.status_code == 403

        ok = await client.get(
            "/api/access/guest-requests",
            params={"zone_id": zone_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body.get("status") == "success"
        data = body.get("data") or []
        assert isinstance(data, list)
        assert any(r.get("guest_id") == guest_id for r in data)
        row = next(r for r in data if r.get("guest_id") == guest_id)
        assert row.get("zone_id") == zone_id
        assert row.get("guest_name") == "List Me"
        assert row.get("expectation") == "unexpected"
        assert row.get("status") == "PENDING"
        assert "created_at" in row

        ap = await client.post(
            "/api/access/approve",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        assert ap.status_code == 200
        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        code = sess.json()["exchange_code"]
        gs = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": code},
        )
        assert gs.status_code == 200
        guest_jwt = gs.json()["data"]["access_token"]

        msg = await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "message": "Welcome guest",
                "type": "CHAT",
                "visibility": "private",
                "zone_id": zone_id,
                "guest_id": guest_id,
            },
        )
        assert msg.status_code == 201, msg.text
        mid = msg.json().get("id")
        assert isinstance(mid, str)

        gl = await client.get(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            params={"zone_id": zone_id, "with_owner_id": admin_id, "limit": 20},
        )
        assert gl.status_code == 200
        items = gl.json()["data"]["items"]
        assert any(i.get("text") == "Welcome guest" for i in items)
