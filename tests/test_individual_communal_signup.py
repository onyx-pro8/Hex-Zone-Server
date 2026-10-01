"""Individual signup with Communal ID → calculated primary + 2 secondaries."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.owner import AccountType, Owner, OwnerRole
from app.models.zone import ZoneType
from app.services.individual_communal_signup import (
    apply_individual_communal_subscription,
    provision_individual_primary_from_communal,
)
from app.services.zone_policy import INDIVIDUAL_SECONDARY_ZONE_LIMIT, build_capabilities


SQLALCHEMY_DATABASE_URL = "sqlite://"
_TEST_PASSWORD_HASH = (
    "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"
)


@pytest.fixture()
def test_db():
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
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


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_individual_secondary_limit_is_two():
    assert INDIVIDUAL_SECONDARY_ZONE_LIMIT == 2
    caps = build_capabilities(
        "user",
        total_zones=0,
        admin_primary_count=1,
        account_type="exclusive",
        is_invited_member=False,
    )
    assert caps.max_total == 2
    assert caps.can_create_primary is False
    assert caps.can_create_secondary is True


def test_provision_primary_merges_multiple_source_zones(test_db, monkeypatch):
    owner = Owner(
        email=_unique("indiv") + "@example.com",
        zone_id=_unique("indiv-net"),
        first_name="Indie",
        last_name="User",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
        hashed_password=_TEST_PASSWORD_HASH,
        api_key=_unique("key"),
        address="1 Main",
        active=True,
    )
    test_db.add(owner)
    test_db.flush()
    owner.account_owner_id = owner.id
    test_db.flush()

    poly = {
        "type": "Polygon",
        "coordinates": [
            [
                [-73.99, 40.73],
                [-73.98, 40.73],
                [-73.98, 40.74],
                [-73.99, 40.74],
                [-73.99, 40.73],
            ]
        ],
    }
    sources = [
        SimpleNamespace(
            id=101,
            name="Park A",
            h3_cells=["8a2a1072b59ffff"],
            geo_fence_polygon=poly,
            parameters={
                "contractType": "geofence",
                "geometry": {"geo_fence_polygon": poly},
                "config": {"h3_cells": ["8a2a1072b59ffff"]},
            },
        ),
        SimpleNamespace(
            id=102,
            name="Park B",
            h3_cells=["8a2a1072b5bffff"],
            geo_fence_polygon=poly,
            parameters={
                "contractType": "geofence",
                "geometry": {"geo_fence_polygon": poly},
                "config": {"h3_cells": ["8a2a1072b5bffff"]},
            },
        ),
    ]

    monkeypatch.setattr(
        "app.services.individual_communal_signup.find_zones_by_communal_id",
        lambda db, reference_id: sources,
    )
    monkeypatch.setattr(
        "app.services.individual_communal_signup.registry_communal_id_taken",
        lambda db, reference_id: True,
    )

    zone = provision_individual_primary_from_communal(test_db, owner, "COMM-PARK")
    assert zone.is_primary is True
    assert zone.zone_type == ZoneType.GEOFENCE
    assert zone.creator_id == owner.id
    cfg = (zone.parameters or {}).get("config") or {}
    assert cfg.get("sourced_from_communal_id") == "COMM-PARK"
    assert cfg.get("is_communal_primary") is True
    assert set(cfg.get("source_zone_ids") or []) == {101, 102}
    assert len(zone.h3_cells or []) == 2


def test_apply_subscription_requires_registered_communal(test_db, monkeypatch):
    owner = Owner(
        email=_unique("indiv") + "@example.com",
        zone_id=_unique("indiv-net"),
        first_name="Indie",
        last_name="User",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
        hashed_password=_TEST_PASSWORD_HASH,
        api_key=_unique("key"),
        address="1 Main",
        active=True,
    )
    test_db.add(owner)
    test_db.flush()
    owner.account_owner_id = owner.id
    test_db.flush()

    monkeypatch.setattr(
        "app.services.individual_communal_signup.registry_communal_id_taken",
        lambda db, reference_id: False,
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        apply_individual_communal_subscription(
            test_db, owner, "NOSUCHID99", required=True
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_exclusive_register_rejects_unknown_communal(
    test_db, override_get_db
):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        reg = await client.post(
            "/register",
            json={
                "name": "Indie User",
                "email": _unique("bad") + "@example.com",
                "password": "SecurePassword123",
                "accountType": "EXCLUSIVE",
                "registrationType": "USER",
                "zoneId": _unique("indiv-net"),
                "communalId": "NOSUCHID99",
                "address": "2 Side St",
                "registrationCode": "FREE",
            },
        )
        assert reg.status_code == 422
