from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import GuestAccessQrToken, ZoneMessageEvent


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


async def _register_admin(client: AsyncClient, zone_id: str) -> str:
    email = f"guest-contract-{uuid.uuid4().hex[:8]}@example.com"
    reg = await client.post(
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
    assert reg.status_code == 201, reg.text
    lr = await client.post("/owners/login", json={"email": email, "password": "SecurePassword123"})
    return lr.json()["access_token"]


@pytest.mark.asyncio
async def test_primary_qr_get_or_create_and_rotate(override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"z-{uuid.uuid4().hex[:6]}"
        token = await _register_admin(client, zone_id)
        headers = {"Authorization": f"Bearer {token}"}
        a = await client.get("/api/access/qr-tokens/primary", params={"zone_id": zone_id}, headers=headers)
        b = await client.get("/api/access/qr-tokens/primary", params={"zone_id": zone_id}, headers=headers)
        assert a.status_code == 200
        assert b.status_code == 200
        assert a.json()["status"] == "success"
        assert a.json()["data"]["id"] == b.json()["data"]["id"]
        assert set(a.json()["data"].keys()) == {
            "id",
            "zone_id",
            "token_suffix",
            "url",
            "path_with_query",
            "revoked_at",
            "created_at",
            "updated_at",
        }
        rot = await client.post(
            "/api/access/qr-tokens/primary/rotate",
            headers=headers,
            json={"zone_id": zone_id, "reason": "security"},
        )
        assert rot.status_code == 200
        assert rot.json()["status"] == "success"
        assert set(rot.json()["data"].keys()) == {
            "id",
            "zone_id",
            "token_suffix",
            "url",
            "path_with_query",
            "revoked_at",
            "created_at",
            "updated_at",
        }
        assert rot.json()["data"]["id"] != a.json()["data"]["id"]
        assert rot.json()["data"]["revoked_at"] is None
        listed = await client.get(
            "/api/access/qr-tokens",
            params={"zone_id": zone_id, "include_revoked": True},
            headers=headers,
        )
        assert listed.status_code == 200
        old = next(x for x in listed.json() if x["id"] == a.json()["data"]["id"])
        new = next(x for x in listed.json() if x["id"] == rot.json()["data"]["id"])
        assert old["revoked_at"] is not None
        assert new["revoked_at"] is None


@pytest.mark.asyncio
async def test_primary_qr_non_expiring_and_old_endpoint_primary_mode(override_get_db, test_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"z-{uuid.uuid4().hex[:6]}"
        token = await _register_admin(client, zone_id)
        headers = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "/api/access/qr-tokens",
            headers=headers,
            json={"zone_id": zone_id, "is_primary": True},
        )
        assert r.status_code == 201, r.text
        assert r.json()["is_primary"] is True
        assert r.json()["expires_at"] is None

        row = (
            test_db.query(GuestAccessQrToken)
            .filter(GuestAccessQrToken.id == r.json()["id"])
            .first()
        )
        assert row is not None
        assert row.expires_at is None
        assert row.is_primary is True

        primary = await client.get("/api/access/qr-tokens/primary", params={"zone_id": zone_id}, headers=headers)
        assert primary.status_code == 200
        assert primary.json()["data"]["id"] == r.json()["id"]

        repeated = await client.post(
            "/api/access/qr-tokens",
            headers=headers,
            json={"zone_id": zone_id, "is_primary": True},
        )
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["id"] == r.json()["id"]

        bad = await client.post(
            "/api/access/qr-tokens",
            headers=headers,
            json={"zone_id": zone_id, "is_primary": True, "expires_in_hours": 12},
        )
        assert bad.status_code == 422
        assert bad.json()["error_code"] == "PRIMARY_TOKEN_EXPIRY_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_access_permission_and_session_envelope(override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"z-{uuid.uuid4().hex[:6]}"
        await _register_admin(client, zone_id)
        perm = await client.post("/api/access/permission", json={"zone_id": zone_id, "guest_name": "Guest A"})
        assert perm.status_code == 200
        assert perm.json()["status"] == "success"
        guest_id = perm.json()["data"]["guest_id"]
        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        assert sess.status_code == 200
        assert sess.json()["status"] == "success"
        assert sess.json()["data"]["status"] == "PENDING"
        assert set(sess.json()["data"].keys()) == {"status", "message"}


@pytest.mark.asyncio
async def test_access_permission_contract_and_token_zone_validation(override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"z-{uuid.uuid4().hex[:6]}"
        token = await _register_admin(client, zone_id)
        headers = {"Authorization": f"Bearer {token}"}

        primary = await client.get("/api/access/qr-tokens/primary", params={"zone_id": zone_id}, headers=headers)
        assert primary.status_code == 200
        # primary endpoint doesn't expose full token; use create route to get a concrete token
        created = await client.post("/api/access/qr-tokens", headers=headers, json={"zone_id": zone_id, "expires_in_hours": 24})
        assert created.status_code == 201
        guest_token = created.json()["token"]

        ok = await client.post(
            "/api/access/permission",
            json={"guest_qr_token": guest_token, "zone_id": zone_id, "guest_name": "Guest One", "sig": "abc"},
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "success"
        assert ok.json()["data"]["status"] in {"EXPECTED", "UNEXPECTED"}
        assert set(ok.json()["data"].keys()) == {"status", "message", "guest_id", "zone_id"}

        mismatch = await client.post(
            "/api/access/permission",
            json={"guest_qr_token": guest_token, "zone_id": "wrong-zone", "guest_name": "Guest Two"},
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["error_code"] == "TOKEN_ZONE_MISMATCH"


@pytest.mark.asyncio
async def test_permission_manual_disabled_and_guest_chat_only(override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"z-{uuid.uuid4().hex[:6]}"
        admin_token = await _register_admin(client, zone_id)
        headers = {"Authorization": f"Bearer {admin_token}"}
        perm_send = await client.post(
            "/messages",
            headers=headers,
            json={"message": "manual", "type": "PERMISSION", "visibility": "private", "guest_id": "x", "zone_id": zone_id},
        )
        assert perm_send.status_code == 422
        assert perm_send.json()["error_code"] == "PERMISSION_MANUAL_DISABLED"


@pytest.mark.asyncio
async def test_message_feature_manual_permission_send_rejected(override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"z-{uuid.uuid4().hex[:6]}"
        admin_token = await _register_admin(client, zone_id)
        headers = {"Authorization": f"Bearer {admin_token}"}
        perm_send = await client.post(
            "/message-feature/messages/propagate",
            headers=headers,
            json={"type": "PERMISSION", "zone_id": zone_id, "text": "manual permission send"},
        )
        assert perm_send.status_code == 422
        assert perm_send.json()["error_code"] == "PERMISSION_MANUAL_DISABLED"


@pytest.mark.asyncio
async def test_permission_events_auto_generated_for_submit_approve_reject(override_get_db, test_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"z-{uuid.uuid4().hex[:6]}"
        admin_token = await _register_admin(client, zone_id)
        headers = {"Authorization": f"Bearer {admin_token}"}

        perm = await client.post("/api/access/permission", json={"zone_id": zone_id, "guest_name": "A Guest"})
        assert perm.status_code == 200
        guest_id = perm.json()["data"]["guest_id"]

        first = (
            test_db.query(ZoneMessageEvent)
            .filter(ZoneMessageEvent.zone_id == zone_id, ZoneMessageEvent.type == "PERMISSION")
            .order_by(ZoneMessageEvent.created_at.asc())
            .all()
        )
        assert len(first) >= 1
        assert first[-1].sender_id is None
        auto_chat = (
            test_db.query(ZoneMessageEvent)
            .filter(ZoneMessageEvent.zone_id == zone_id, ZoneMessageEvent.type == "CHAT")
            .all()
        )
        assert auto_chat == []

        req = await client.get("/api/access/guest-requests", params={"zone_id": zone_id}, headers=headers)
        rid = req.json()["data"][0]["id"]
        ap = await client.post(f"/message-feature/access/guest-requests/{rid}/approve", params={"zone_id": zone_id}, headers=headers)
        assert ap.status_code == 200
        ap_body = ap.json()
        assert ap_body["status"] == "success"
        assert set(ap_body["data"].keys()) == {"id", "status", "zone_id", "updated_at"}
        assert ap_body["data"]["id"] == str(rid)
        assert ap_body["data"]["status"] == "APPROVED"
        assert ap_body["data"]["zone_id"] == zone_id

        perm2 = (
            test_db.query(ZoneMessageEvent)
            .filter(ZoneMessageEvent.zone_id == zone_id, ZoneMessageEvent.type == "PERMISSION")
            .order_by(ZoneMessageEvent.created_at.desc())
            .first()
        )
        assert perm2 is not None
        assert perm2.sender_id is None

        perm_b = await client.post("/api/access/permission", json={"zone_id": zone_id, "guest_name": "B Guest"})
        req_b = await client.get("/api/access/guest-requests", params={"zone_id": zone_id}, headers=headers)
        rid_b = next(x["id"] for x in req_b.json()["data"] if x["guest_id"] == perm_b.json()["data"]["guest_id"])
        rj = await client.post(f"/message-feature/access/guest-requests/{rid_b}/reject", params={"zone_id": zone_id}, headers=headers)
        assert rj.status_code == 200
        rj_body = rj.json()
        assert rj_body["status"] == "success"
        assert set(rj_body["data"].keys()) == {"id", "status", "zone_id", "updated_at"}
        assert rj_body["data"]["id"] == str(rid_b)
        assert rj_body["data"]["status"] == "REJECTED"
        assert rj_body["data"]["zone_id"] == zone_id


@pytest.mark.asyncio
async def test_message_feature_approve_reject_zone_query_required(override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"z-{uuid.uuid4().hex[:6]}"
        admin_token = await _register_admin(client, zone_id)
        headers = {"Authorization": f"Bearer {admin_token}"}

        perm = await client.post("/api/access/permission", json={"zone_id": zone_id, "guest_name": "Guest Optional Zone"})
        assert perm.status_code == 200
        guest_id = perm.json()["data"]["guest_id"]

        req = await client.get("/api/access/guest-requests", params={"zone_id": zone_id}, headers=headers)
        rid = req.json()["data"][0]["id"]

        ap = await client.post(f"/message-feature/access/guest-requests/{rid}/approve", headers=headers)
        assert ap.status_code == 422, ap.text

        ap_ok = await client.post(
            f"/message-feature/access/guest-requests/{rid}/approve",
            params={"zone_id": zone_id},
            headers=headers,
        )
        assert ap_ok.status_code == 200, ap_ok.text
        assert ap_ok.json()["data"]["status"] == "APPROVED"
        assert ap_ok.json()["data"]["zone_id"] == zone_id

        perm2 = await client.post("/api/access/permission", json={"zone_id": zone_id, "guest_name": "Guest Optional Zone 2"})
        assert perm2.status_code == 200
        guest_id_2 = perm2.json()["data"]["guest_id"]
        req2 = await client.get("/api/access/guest-requests", params={"zone_id": zone_id}, headers=headers)
        rid2 = next(x["id"] for x in req2.json()["data"] if x["guest_id"] == guest_id_2)

        rj = await client.post(f"/message-feature/access/guest-requests/{rid2}/reject", headers=headers)
        assert rj.status_code == 422, rj.text

        rj_ok = await client.post(
            f"/message-feature/access/guest-requests/{rid2}/reject",
            params={"zone_id": zone_id},
            headers=headers,
        )
        assert rj_ok.status_code == 200, rj_ok.text
        assert rj_ok.json()["data"]["status"] == "REJECTED"
        assert rj_ok.json()["data"]["zone_id"] == zone_id


@pytest.mark.asyncio
async def test_guest_history_includes_permission_system_events(override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"z-{uuid.uuid4().hex[:6]}"
        admin_token = await _register_admin(client, zone_id)
        headers = {"Authorization": f"Bearer {admin_token}"}

        perm = await client.post("/api/access/permission", json={"zone_id": zone_id, "guest_name": "Perm Visible"})
        assert perm.status_code == 200
        guest_id = perm.json()["data"]["guest_id"]

        req = await client.get("/api/access/guest-requests", params={"zone_id": zone_id}, headers=headers)
        rid = next(x["id"] for x in req.json()["data"] if x["guest_id"] == guest_id)
        ap = await client.post(f"/message-feature/access/guest-requests/{rid}/approve", params={"zone_id": zone_id}, headers=headers)
        assert ap.status_code == 200

        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        assert sess.status_code == 200
        exchange_code = sess.json()["data"]["exchange_code"]
        guest_session = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": exchange_code},
        )
        assert guest_session.status_code == 200
        guest_jwt = guest_session.json()["data"]["access_token"]

        hist = await client.get(
            "/api/guest/messages",
            headers={"Authorization": f"Bearer {guest_jwt}"},
            params={"zone_id": zone_id, "limit": 50},
        )
        assert hist.status_code == 200
        items = hist.json()["data"]["items"]
        types = {i["type"] for i in items}
        assert "PERMISSION" in types


@pytest.mark.asyncio
async def test_guest_role_read_only_boundaries_for_admin_endpoints(override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        zone_id = f"z-{uuid.uuid4().hex[:6]}"
        admin_token = await _register_admin(client, zone_id)
        headers = {"Authorization": f"Bearer {admin_token}"}

        perm = await client.post("/api/access/permission", json={"zone_id": zone_id, "guest_name": "Read Only Guest"})
        assert perm.status_code == 200
        guest_id = perm.json()["data"]["guest_id"]

        ap = await client.post(
            "/api/access/approve",
            headers=headers,
            json={"guest_id": guest_id, "zone_id": zone_id},
        )
        assert ap.status_code == 200
        sess = await client.get(f"/api/access/session/{guest_id}", params={"zone_id": zone_id})
        exchange_code = sess.json()["data"]["exchange_code"]
        guest_session = await client.post(
            "/api/access/guest-session",
            json={"guest_id": guest_id, "zone_id": zone_id, "exchange_code": exchange_code},
        )
        assert guest_session.status_code == 200
        guest_jwt = guest_session.json()["data"]["access_token"]
        guest_headers = {"Authorization": f"Bearer {guest_jwt}"}

        # Guest must not access/administer QR token lifecycle endpoints.
        primary = await client.get("/api/access/qr-tokens/primary", params={"zone_id": zone_id}, headers=guest_headers)
        rotate = await client.post(
            "/api/access/qr-tokens/primary/rotate",
            json={"zone_id": zone_id, "reason": "nope"},
            headers=guest_headers,
        )
        assert primary.status_code in {401, 403}
        assert rotate.status_code in {401, 403}

        # Guest cannot administer access requests.
        req_list = await client.get("/api/access/guest-requests", params={"zone_id": zone_id}, headers=guest_headers)
        decide = await client.post(
            "/message-feature/access/guest-requests/does-not-matter/approve",
            params={"zone_id": zone_id},
            headers=guest_headers,
        )
        assert req_list.status_code in {401, 403}
        assert decide.status_code in {401, 403}
