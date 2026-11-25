"""Forgot-password email codes and signed-in password changes."""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.owner import AccountType, Owner, OwnerRole
from app.models.password_reset import PasswordReset


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


@pytest.fixture()
def captured_codes(monkeypatch):
    sent: dict[str, str] = {}

    def _send(*, to_email: str, code: str):
        sent[to_email.strip().lower()] = code
        return {"sent": True, "delivery": "resend", "reason": None}

    def _hash(password: str) -> str:
        return f"hash:{password}"

    def _verify(plain: str, hashed: str) -> bool:
        return hashed == f"hash:{plain}"

    monkeypatch.setattr(
        "app.services.password_reset_service.send_password_reset_email",
        _send,
    )
    monkeypatch.setattr(
        "app.services.password_reset_service.get_password_hash",
        _hash,
    )
    monkeypatch.setattr(
        "app.services.password_reset_service.verify_password",
        _verify,
    )
    return sent


def _owner(db, email: str) -> Owner:
    owner = Owner(
        email=email,
        zone_id=f"Network-{uuid.uuid4().hex[:6].upper()}",
        first_name="Avery",
        last_name="Stone",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
        hashed_password="hash:OldPassword1",
        api_key=uuid.uuid4().hex,
        address="101 Main St",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(owner)
    db.commit()
    return owner


def test_unknown_email_is_rejected(test_db, captured_codes):
    from app.services.password_reset_service import start_password_reset

    with pytest.raises(HTTPException) as exc:
        start_password_reset(test_db, "missing@example.com")
    assert exc.value.status_code == 404
    assert captured_codes == {}


def test_confirm_then_reset_replaces_the_password(test_db, captured_codes):
    from app.services.password_reset_service import (
        complete_password_reset,
        confirm_password_reset,
        start_password_reset,
    )

    email = f"reset-{uuid.uuid4().hex[:8]}@example.com"
    owner = _owner(test_db, email)
    started = start_password_reset(test_db, email)
    test_db.commit()
    assert started["sent"] is True
    assert owner.hashed_password == "hash:OldPassword1"

    confirmed = confirm_password_reset(test_db, email, captured_codes[email.lower()])
    test_db.commit()
    assert confirmed["reset_token"]
    complete_password_reset(test_db, email, confirmed["reset_token"], "NewPassword9")
    test_db.commit()
    test_db.refresh(owner)
    assert owner.hashed_password == "hash:NewPassword9"
    assert test_db.query(PasswordReset).filter(PasswordReset.email == email.lower()).first() is None


def test_wrong_code_does_not_change_the_password(test_db, captured_codes):
    from app.services.password_reset_service import confirm_password_reset, start_password_reset

    email = f"wrong-{uuid.uuid4().hex[:8]}@example.com"
    owner = _owner(test_db, email)
    start_password_reset(test_db, email)
    test_db.commit()
    bad = "0000" if captured_codes[email.lower()] != "0000" else "0001"
    with pytest.raises(HTTPException) as exc:
        confirm_password_reset(test_db, email, bad)
    assert exc.value.status_code == 400
    test_db.refresh(owner)
    assert owner.hashed_password == "hash:OldPassword1"


def test_change_password_requires_the_current_password(test_db, captured_codes):
    from app.services.password_reset_service import change_password

    email = f"change-{uuid.uuid4().hex[:8]}@example.com"
    owner = _owner(test_db, email)
    with pytest.raises(HTTPException) as exc:
        change_password(test_db, owner, "not-the-password", "NewPassword9")
    assert exc.value.status_code == 400
    updated = change_password(test_db, owner, "OldPassword1", "NewPassword9")
    assert updated["updated"] is True
    assert owner.hashed_password == "hash:NewPassword9"


@pytest.mark.asyncio
async def test_http_forgot_password_flow(test_db, override_get_db, captured_codes):
    email = f"http-{uuid.uuid4().hex[:8]}@example.com"
    _owner(test_db, email)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        started = await client.post("/auth/password/forgot", json={"email": email})
        assert started.status_code == 200, started.text
        body = started.json()["data"]
        assert body["sent"] is True
        assert "code" not in body

        confirmed = await client.post(
            "/auth/password/forgot/confirm",
            json={"email": email, "code": captured_codes[email.lower()]},
        )
        assert confirmed.status_code == 200, confirmed.text
        token = confirmed.json()["data"]["reset_token"]

        reset = await client.post(
            "/auth/password/reset",
            json={"email": email, "reset_token": token, "password": "NewPassword9"},
        )
        assert reset.status_code == 200, reset.text
        assert reset.json()["data"]["updated"] is True
