"""Guest CHAT peers include all network members, not only admins / zone owners."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.services.guest_api_service import list_zone_peers_for_guest


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _owner(db, *, oid: int, network: str, role: OwnerRole, name: str) -> Owner:
    row = Owner(
        id=oid,
        email=f"u{oid}@test.com",
        zone_id=network,
        first_name=name,
        last_name="User",
        account_type=AccountType.PRIVATE,
        role=role,
        account_owner_id=1 if role == OwnerRole.USER and oid != 1 else None,
        hashed_password="x",
        api_key=f"key-{oid}",
        address="addr",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def test_guest_peers_include_admin_and_invited_user(db):
    network = "NET-PEERS-1"
    _owner(db, oid=1, network=network, role=OwnerRole.ADMINISTRATOR, name="Admin")
    _owner(db, oid=2, network=network, role=OwnerRole.USER, name="Member")
    db.commit()

    peers = list_zone_peers_for_guest(db, zone_id=network)
    ids = {p["owner_id"] for p in peers}
    assert ids == {1, 2}
