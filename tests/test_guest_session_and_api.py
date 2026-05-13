"""Approved-guest exchange, JWT, and guest-only messaging API."""
from __future__ import annotations

import uuid
import secrets

import pytest
from httpx import AsyncClient

from app.main import app
from app.database import Base, get_db
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models.owner import AccountType, Owner, OwnerRole


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
        guest_id = perm.json()["data"]["guest_id"]
        zone_id = perm.json()["data"]["zone_id"]

        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        assert sess.status_code == 200
        sj = sess.json()
        assert sj["status"] == "success"
        assert sj["data"]["status"] == "PENDING"
        assert "exchange_code" not in sj["data"]

        ap = await client.post(
            "/api/access/approve",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        assert ap.status_code == 200

        sess2 = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        assert sess2.status_code == 200
        body = sess2.json()["data"]
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
                "type": "PERMISSION",
                "text": "manual permission",
                "to_owner_id": admin_id,
            },
        )
        assert bad_msg.status_code == 422
        assert bad_msg.json().get("error_code") == "PERMISSION_MANUAL_DISABLED"

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
        assert bad_msg.status_code == 422
        assert bad_msg.json().get("error_code") == "GUEST_MESSAGE_TYPE_NOT_ALLOWED"

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

        rv = await client.post(
            "/api/access/reject",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        assert rv.status_code == 200, rv.text

        me2 = await client.get("/api/guest/me", headers={"Authorization": f"Bearer {guest_jwt}"})
        assert me2.status_code == 401
        assert me2.json().get("error_code") == "GUEST_ACCESS_INVALIDATED"

        peers2 = await client.get(
            f"/api/guest/zones/{zone_id}/peers",
            headers={"Authorization": f"Bearer {guest_jwt}"},
        )
        assert peers2.status_code == 401


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
        guest_id = perm.json()["data"]["guest_id"]
        zone_id = perm.json()["data"]["zone_id"]
        await client.post(
            "/api/access/approve",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        code = sess.json()["data"]["exchange_code"]
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
async def test_guest_chat_mirrors_to_admin_messages_inbox_and_symmetric_thread(test_db, override_get_db):
    """Guest POST CHAT → **`GET /messages?owner_id=`** includes **CHAT**; member reply visible to guest thread (newest first)."""

    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_id, admin_token = await _register_admin(client, zone_id="zone-guest-inbox-chat")

        perm = await client.post(
            "/api/access/permission",
            json={"zone_id": "zone-guest-inbox-chat", "guest_name": "Inbox Guest"},
        )
        guest_id = perm.json()["data"]["guest_id"]
        zone_id = perm.json()["data"]["zone_id"]
        await client.post(
            "/api/access/approve",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        code = sess.json()["data"]["exchange_code"]
        gs = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": code},
        )
        guest_jwt = gs.json()["data"]["access_token"]

        await client.post(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            json={"zone_id": zone_id, "type": "CHAT", "text": "Guest line one", "to_owner_id": admin_id},
        )

        inbox = await client.get(
            "/messages",
            params={"owner_id": admin_id, "skip": 0, "limit": 100},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert inbox.status_code == 200, inbox.text
        items = inbox.json()
        chats = [m for m in items if isinstance(m.get("id"), str) and m.get("type") == "CHAT"]
        ours = next((m for m in chats if m.get("message") == "Guest line one"), None)
        assert ours is not None
        assert ours.get("receiver_id") == admin_id
        assert ours.get("sender_id") is None
        assert ours.get("guest_id") == guest_id
        assert ours.get("zone_id") == zone_id

        rep = await client.post(
            "/messages/",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "zone_id": zone_id,
                "guest_id": guest_id,
                "message": "Admin reply ok",
                "type": "CHAT",
                "visibility": "private",
            },
        )
        assert rep.status_code == 201, rep.text
        rj = rep.json()
        assert rj.get("type") == "CHAT"
        assert rj.get("guest_id") == guest_id

        gst = await client.get(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            params={"zone_id": zone_id, "with_owner_id": admin_id, "limit": 50},
        )
        assert gst.status_code == 200
        git = gst.json()["data"]["items"]
        texts = [i["text"] for i in git]
        assert "Admin reply ok" in texts
        assert "Guest line one" in texts
        # Newest-first within this peer thread
        assert texts[0] == "Admin reply ok"


@pytest.mark.asyncio
async def test_guest_chat_inbox_mirror_respects_disable_flag(test_db, override_get_db):
    from app.core.config import settings

    prev = settings.MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT
    try:
        setattr(settings, "MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT", False)
        async with AsyncClient(app=app, base_url="http://test") as client:
            admin_id, admin_token = await _register_admin(client, zone_id="zone-guest-inbox-flag")
            perm = await client.post(
                "/api/access/permission",
                json={"zone_id": "zone-guest-inbox-flag", "guest_name": "Flag Guest"},
            )
            gid = perm.json()["data"]["guest_id"]
            zid = perm.json()["data"]["zone_id"]
            await client.post(
                "/api/access/approve",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"guest_id": gid, "zone_id": zid},
            )
            sess = await client.get(f"/api/access/session/{gid}", params={"zone_id": zid})
            code = sess.json()["data"]["exchange_code"]
            gs = await client.post(
                "/api/access/guest-session",
                json={"guest_id": gid, "zone_id": zid, "exchange_code": code},
            )
            gj = gs.json()["data"]["access_token"]
            await client.post(
                "/api/guest/messages",
                headers={"Authorization": f"Bearer {gj}"},
                json={"zone_id": zid, "type": "CHAT", "text": "Hidden from merge", "to_owner_id": admin_id},
            )
            inbox = await client.get(
                "/messages",
                params={"owner_id": admin_id, "skip": 0, "limit": 100},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert inbox.status_code == 200
            assert not any(
                m.get("type") == "CHAT" and m.get("message") == "Hidden from merge" for m in inbox.json()
            )
    finally:
        setattr(settings, "MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT", prev)


@pytest.mark.asyncio
async def test_guest_zone_scope_enforced_for_read_and_write(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        admin_id, admin_token = await _register_admin(client, zone_id="zone-guest-scope")
        perm = await client.post(
            "/api/access/permission",
            json={"zone_id": "zone-guest-scope", "guest_name": "Scoped Visitor"},
        )
        guest_id = perm.json()["data"]["guest_id"]
        zone_id = perm.json()["data"]["zone_id"]
        await client.post(
            "/api/access/approve",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        code = sess.json()["data"]["exchange_code"]
        gs = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": code},
        )
        guest_jwt = gs.json()["data"]["access_token"]

        wrong_zone_read = await client.get(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            params={"zone_id": "other-zone"},
        )
        assert wrong_zone_read.status_code == 403
        assert wrong_zone_read.json()["error_code"] == "GUEST_NOT_AUTHORIZED_FOR_ZONE"

        wrong_zone_write = await client.post(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            json={"zone_id": "other-zone", "type": "CHAT", "text": "Nope", "to_owner_id": admin_id},
        )
        assert wrong_zone_write.status_code == 403
        assert wrong_zone_write.json()["error_code"] == "GUEST_NOT_AUTHORIZED_FOR_ZONE"


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
        guest_id = perm.json()["data"]["guest_id"]

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
        code = sess.json()["data"]["exchange_code"]
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

        msg_alias = await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "message": "message_type alias",
                "message_type": "CHAT",
                "visibility": "private",
                "zone_id": zone_id,
                "guest_id": guest_id,
            },
        )
        assert msg_alias.status_code == 201, msg_alias.text

        gl = await client.get(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            params={"zone_id": zone_id, "with_owner_id": admin_id, "limit": 20},
        )
        assert gl.status_code == 200
        items = gl.json()["data"]["items"]
        texts = {i.get("text") for i in items}
        assert "Welcome guest" in texts
        assert "message_type alias" in texts


@pytest.mark.asyncio
async def test_admin_can_see_access_permission_and_chat_messages(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"zone-admin-thread-{uuid.uuid4().hex[:8]}"
        admin_id, admin_token = await _register_admin(client, zone_id=zone_id)
        headers = {"Authorization": f"Bearer {admin_token}"}

        perm = await client.post("/api/access/permission", json={"zone_id": zone_id, "guest_name": "Thread Guest"})
        assert perm.status_code == 200
        guest_id = perm.json()["data"]["guest_id"]

        approve = await client.post(
            "/api/access/approve",
            headers=headers,
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        assert approve.status_code == 200

        send_chat = await client.post(
            "/messages",
            headers=headers,
            json={
                "message": "Welcome from admin",
                "type": "CHAT",
                "visibility": "private",
                "zone_id": zone_id,
                "guest_id": guest_id,
            },
        )
        assert send_chat.status_code == 201, send_chat.text

        admin_thread = await client.get(
            "/messages",
            headers=headers,
            params={"owner_id": admin_id, "guest_id": guest_id, "zone_id": zone_id, "limit": 50},
        )
        assert admin_thread.status_code == 200, admin_thread.text
        rows = admin_thread.json()
        assert any(r["type"] == "PERMISSION" for r in rows)
        assert any(r["type"] == "CHAT" and r["message"] == "Welcome from admin" for r in rows)

        api_access = await client.get(
            "/api/access/guest-messages",
            headers=headers,
            params={"guest_id": guest_id, "zone_id": zone_id, "limit": 50},
        )
        assert api_access.status_code == 200, api_access.text
        envelope = api_access.json()
        assert envelope["status"] == "success"
        items = envelope["data"]["items"]
        assert any(i["type"] == "PERMISSION" for i in items)
        assert any(i["type"] == "CHAT" and i["message"] == "Welcome from admin" for i in items)

        gr = await client.get("/api/access/guest-requests", params={"zone_id": zone_id}, headers=headers)
        assert gr.status_code == 200
        session_row_id = next(x["id"] for x in gr.json()["data"] if x["guest_id"] == guest_id)

        by_session_id_only = await client.get(
            "/messages",
            headers=headers,
            params={"owner_id": admin_id, "requestId": session_row_id, "limit": 50},
        )
        assert by_session_id_only.status_code == 200, by_session_id_only.text
        rows_sess = by_session_id_only.json()
        assert any(r["type"] == "PERMISSION" for r in rows_sess)

        camel = await client.get(
            "/messages",
            headers=headers,
            params={"owner_id": admin_id, "guestId": guest_id, "zoneId": zone_id, "limit": 50},
        )
        assert camel.status_code == 200, camel.text
        rows_camel = camel.json()
        assert any(r["type"] == "CHAT" for r in rows_camel)


@pytest.mark.asyncio
async def test_guest_messaging_restricted_to_host_admin_peers(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"zone-peer-{uuid.uuid4().hex[:8]}"
        admin_id, admin_token = await _register_admin(client, zone_id=zone_id)

        non_admin = Owner(
            email=f"user-{uuid.uuid4().hex[:8]}@example.com",
            zone_id=zone_id,
            first_name="Zone",
            last_name="User",
            account_type=AccountType.PRIVATE,
            role=OwnerRole.USER,
            account_owner_id=None,
            hashed_password="not-used-in-test",
            api_key=secrets.token_urlsafe(24),
            phone=None,
            address="Addr",
            active=True,
            expired=False,
        )
        test_db.add(non_admin)
        test_db.commit()
        test_db.refresh(non_admin)

        perm = await client.post("/api/access/permission", json={"zone_id": zone_id, "guest_name": "Peer Filter"})
        assert perm.status_code == 200
        guest_id = perm.json()["data"]["guest_id"]

        approve = await client.post(
            "/api/access/approve",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        assert approve.status_code == 200
        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        exchange_code = sess.json()["data"]["exchange_code"]
        gs = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": exchange_code},
        )
        guest_jwt = gs.json()["data"]["access_token"]

        peers = await client.get(
            f"/api/guest/zones/{zone_id}/peers",
            headers={"Authorization": f"Bearer {guest_jwt}"},
        )
        assert peers.status_code == 200
        peer_ids = {p["owner_id"] for p in peers.json()["data"]["peers"]}
        assert admin_id in peer_ids
        assert non_admin.id not in peer_ids

        blocked = await client.post(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            json={"zone_id": zone_id, "type": "CHAT", "text": "Should fail", "to_owner_id": non_admin.id},
        )
        assert blocked.status_code == 403
        assert blocked.json().get("error_code") == "GUEST_NOT_AUTHORIZED_FOR_ZONE"


@pytest.mark.asyncio
async def test_expected_schedule_guest_permission_poll_and_guest_session(test_db, override_get_db):
    """EXPECTED (schedule) arrivals expose exchange on permission and poll; guest-session accepts."""
    from datetime import datetime, timedelta

    from app.models import AccessSchedule

    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = "zone-sched-exch-1"
        await _register_admin(client, zone_id=zone_id)
        sched = AccessSchedule(
            zone_id=zone_id,
            guest_name="Sched Pat",
            event_id=None,
            starts_at=datetime.utcnow() - timedelta(hours=1),
            ends_at=datetime.utcnow() + timedelta(hours=1),
            active=True,
            notify_member_assist=False,
        )
        test_db.add(sched)
        test_db.commit()

        perm = await client.post(
            "/api/access/permission",
            json={"zone_id": zone_id, "guest_name": "Sched Pat"},
        )
        assert perm.status_code == 200
        pj = perm.json()["data"]
        assert pj["status"] == "EXPECTED"
        assert pj.get("exchange_code")
        assert pj.get("exchange_expires_at")
        guest_id = pj["guest_id"]

        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        sd = sess.json()["data"]
        assert sd["status"] == "APPROVED"
        assert sd["exchange_code"] == pj["exchange_code"]
        assert sd["exchange_expires_at"] == pj["exchange_expires_at"]

        gs = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": pj["exchange_code"]},
        )
        assert gs.status_code == 200
        assert gs.json()["data"]["guest"]["guest_id"] == guest_id

        gs2 = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": pj["exchange_code"]},
        )
        assert gs2.status_code == 409


@pytest.mark.asyncio
async def test_schedule_evt_event_id_matches_bare_digits_on_permission(test_db, override_get_db):
    """Schedule stored as EVT-1234; guest sends event_id 1234 → same EXPECTED outcome."""
    from datetime import datetime, timedelta

    from app.models import AccessSchedule

    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = "zone-evt-canonical-1"
        await _register_admin(client, zone_id=zone_id)
        sched = AccessSchedule(
            zone_id=zone_id,
            guest_name="Other Person",
            event_id="EVT-1234",
            starts_at=datetime.utcnow() - timedelta(hours=1),
            ends_at=datetime.utcnow() + timedelta(hours=1),
            active=True,
            notify_member_assist=False,
        )
        test_db.add(sched)
        test_db.commit()

        perm = await client.post(
            "/api/access/permission",
            json={
                "zone_id": zone_id,
                "guest_name": "Walk-in",
                "event_id": "1234",
            },
        )
        assert perm.status_code == 200
        assert perm.json()["data"]["status"] == "EXPECTED"
        assert perm.json()["data"]["guest_id"]
