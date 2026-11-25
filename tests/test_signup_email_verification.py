"""Public signup emails a 4-digit code and creates the account only after confirm."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.owner import Owner
from app.models.pending_signup import PendingSignup


@pytest.fixture()
def test_db():
    engine = create_engine(
        "sqlite://",
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


def _signup_body(email: str | None = None) -> dict:
    return {
        "name": "Avery Stone",
        "email": email or f"verify-{uuid.uuid4().hex[:8]}@example.com",
        "password": "SecurePassword123",
        "accountType": "EXCLUSIVE",
        "registrationType": "USER",
        "zoneId": f"Network-{uuid.uuid4().hex[:6].upper()}",
        "address": "101 Main St, Denver, CO, USA",
    }


@pytest.fixture()
def captured_codes(monkeypatch):
    sent: dict[str, str] = {}

    def _send(*, to_email: str, code: str):
        sent[to_email.strip().lower()] = code
        return {"sent": True, "delivery": "resend", "reason": None}

    monkeypatch.setattr(
        "app.services.signup_verification_service.send_signup_verification_email",
        _send,
    )
    monkeypatch.setattr(
        "app.services.auth_service.sync_owner_home_from_address",
        lambda _owner: None,
    )
    monkeypatch.setattr(
        "app.services.auth_service.get_password_hash",
        lambda _password: "hashed",
    )
    monkeypatch.setattr(
        "app.services.signup_verification_service.get_password_hash",
        lambda _password: "hashed",
    )
    return sent


def test_start_stores_code_without_the_password(test_db, captured_codes):
    from app.services.signup_verification_service import start_signup_verification

    body = _signup_body()
    result = start_signup_verification(test_db, body)
    test_db.commit()

    assert result["sent"] is True
    assert result["delivery"] == "resend"
    assert captured_codes[body["email"]] .isdigit() and len(captured_codes[body["email"]]) == 4
    row = test_db.query(PendingSignup).filter(PendingSignup.email == body["email"]).one()
    stored = json.loads(row.payload_json)
    assert "password" not in stored
    assert body["password"] not in row.payload_json
    assert row.password_hash
    assert row.password_hash != body["password"]
    assert test_db.query(Owner).filter(Owner.email == body["email"]).first() is None


def test_confirm_creates_the_account(test_db, captured_codes):
    from app.services.signup_verification_service import (
        confirm_signup_verification,
        start_signup_verification,
    )

    body = _signup_body()
    start_signup_verification(test_db, body)
    test_db.commit()
    created = confirm_signup_verification(test_db, body["email"], captured_codes[body["email"]])
    test_db.commit()

    assert created["email"] == body["email"]
    assert test_db.query(Owner).filter(Owner.email == body["email"]).one()
    assert test_db.query(PendingSignup).filter(PendingSignup.email == body["email"]).first() is None


def test_wrong_code_does_not_create_an_account(test_db, captured_codes):
    from app.services.signup_verification_service import (
        confirm_signup_verification,
        start_signup_verification,
    )

    body = _signup_body()
    start_signup_verification(test_db, body)
    test_db.commit()
    with pytest.raises(HTTPException) as exc:
        confirm_signup_verification(test_db, body["email"], "0000" if captured_codes[body["email"]] != "0000" else "0001")
    assert exc.value.status_code == 400
    assert test_db.query(Owner).filter(Owner.email == body["email"]).first() is None


def test_resend_is_rate_limited_until_the_cooldown_passes(test_db, captured_codes):
    from app.services.signup_verification_service import (
        resend_signup_verification,
        start_signup_verification,
    )

    body = _signup_body()
    start_signup_verification(test_db, body)
    test_db.commit()
    again = resend_signup_verification(test_db, body["email"])
    assert again["delivery"] == "cooldown"
    assert again["sent"] is False
    assert again["resend_after_seconds"] > 0


@pytest.mark.asyncio
async def test_http_verification_flow(test_db, override_get_db, captured_codes):
    body = _signup_body()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        started = await client.post("/auth/signup/verification", json=body)
        assert started.status_code == 200, started.text
        payload = started.json()["data"]
        assert payload["sent"] is True
        assert "code" not in payload

        confirmed = await client.post(
            "/auth/signup/verification/confirm",
            json={"email": body["email"], "code": captured_codes[body["email"]]},
        )
        assert confirmed.status_code == 201, confirmed.text
        assert confirmed.json()["data"]["email"] == body["email"]
