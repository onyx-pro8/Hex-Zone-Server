"""Universal access invite keys and place-access requests."""
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.services import universal_access_service

PHOTO = "data:image/png;base64,iVBORw0KGgo="
BUSINESS = "data:application/pdf;base64,JVBERi0="


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


def _owner(db, *, email, zone_id, account_type, role, account_owner_id=None):
    owner = Owner(
        email=email,
        zone_id=zone_id,
        first_name="Ada",
        last_name="Lovelace",
        account_type=account_type,
        role=role,
        account_owner_id=account_owner_id,
        hashed_password="x",
        api_key=f"key-{email}",
        address="1 Main",
        active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(owner)
    db.flush()
    if account_owner_id is None and role == OwnerRole.ADMINISTRATOR:
        owner.account_owner_id = owner.id
    db.flush()
    return owner


def _insert_zone(
    db,
    *,
    owner_id: int,
    zone_key: str,
    name: str,
    is_primary: bool = True,
    parameters: str = "{}",
) -> int:
    db.execute(
        text(
            """
            INSERT INTO zones (
                zone_id, owner_id, creator_id, zone_type, name, h3_cells,
                parameters, active, is_primary, created_at, updated_at
            ) VALUES (
                :zone_key, :owner_id, :owner_id, 'GEOFENCE', :name, '[]',
                :parameters, 1, :is_primary, :now, :now
            )
            """
        ),
        {
            "zone_key": zone_key,
            "owner_id": owner_id,
            "name": name,
            "parameters": parameters,
            "is_primary": 1 if is_primary else 0,
            "now": datetime.utcnow().isoformat(sep=" "),
        },
    )
    db.flush()
    return int(db.execute(text("SELECT id FROM zones WHERE zone_id = :zid"), {"zid": zone_key}).scalar())


def test_private_invite_generates_key_immediately(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    invitee = _owner(
        db,
        email="worker@example.com",
        zone_id="NET-B",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    invitee.account_owner_id = invitee.id
    shared_a = _insert_zone(db, owner_id=admin.id, zone_key="NET-A", name="City hall")
    # Fix: zone_id on zones table is network id string; use unique keys that match admin network
    db.execute(text("UPDATE zones SET zone_id = 'NET-A' WHERE id = :id"), {"id": shared_a})
    shared_b = _insert_zone(db, owner_id=admin.id, zone_key="depot-b", name="Depot")
    db.execute(text("UPDATE zones SET zone_id = 'NET-A' WHERE id = :id"), {"id": shared_b})
    _insert_zone(db, owner_id=admin.id, zone_key="zone-c", name="Secondary only", is_primary=False)
    db.commit()

    created = universal_access_service.generate_invite_key(
        db,
        admin=admin,
        key_type="private",
        invitee_email=invitee.email,
        zone_ids=[shared_a, shared_b],
    )
    assert created["request_kind"] == "invite"
    assert created["status"] == "pending"
    assert created["key_type"] == "private"
    assert created["code"] is None

    before = universal_access_service.universal_primary_zones(db, invitee, already_ids=set())
    assert before == []

    invitee_list = universal_access_service.list_requests(db, caller=invitee)
    assert len(invitee_list) == 1
    assert invitee_list[0]["viewer_actions"]["can_approve"] is True
    assert invitee_list[0]["viewer_actions"]["can_reject"] is True
    assert invitee_list[0]["viewer_actions"]["can_remove"] is False

    approved = universal_access_service.approve_request(
        db, caller=invitee, request_id=created["id"]
    )
    assert approved["status"] == "approved"
    assert approved["code"]
    visible = universal_access_service.universal_primary_zones(db, invitee, already_ids=set())
    assert [row.id for row in visible] == [shared_a, shared_b]


def test_private_invite_reject_does_not_add_zones(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    invitee = _owner(
        db,
        email="worker@example.com",
        zone_id="NET-B",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    invitee.account_owner_id = invitee.id
    shared = _insert_zone(db, owner_id=admin.id, zone_key="hall", name="Hall")
    db.execute(text("UPDATE zones SET zone_id = 'NET-A' WHERE id = :id"), {"id": shared})
    db.commit()

    created = universal_access_service.generate_invite_key(
        db,
        admin=admin,
        key_type="private",
        invitee_email=invitee.email,
        zone_ids=[shared],
    )
    rejected = universal_access_service.reject_request(
        db,
        caller=invitee,
        request_id=created["id"],
        reason="Not my site",
    )
    assert rejected["status"] == "rejected"
    assert rejected["code"] is None
    assert universal_access_service.universal_primary_zones(db, invitee, already_ids=set()) == []
    assert universal_access_service.list_private_keys(db, caller=invitee) == []


def test_public_invite_activates_on_zones_page(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    outsider = _owner(
        db,
        email="out@example.com",
        zone_id="NET-B",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    outsider.account_owner_id = outsider.id
    shared = _insert_zone(db, owner_id=admin.id, zone_key="hall", name="City hall")
    db.execute(text("UPDATE zones SET zone_id = 'NET-A' WHERE id = :id"), {"id": shared})
    db.commit()

    created = universal_access_service.generate_invite_key(
        db,
        admin=admin,
        key_type="public",
        zone_ids=[shared],
    )
    assert created["key_type"] == "public"
    assert created["code"]

    before = universal_access_service.universal_primary_zones(db, outsider, already_ids=set())
    assert before == []

    activated = universal_access_service.activate_public_key(
        db, caller=outsider, code=created["code"]
    )
    assert activated["credential_id"] == created["credential_id"]
    visible = universal_access_service.universal_primary_zones(db, outsider, already_ids=set())
    assert [row.id for row in visible] == [shared]

    removed = universal_access_service.deactivate_public_key(
        db, caller=outsider, credential_id=created["credential_id"]
    )
    assert removed["credential_id"] == created["credential_id"]
    after_remove = universal_access_service.universal_primary_zones(
        db, outsider, already_ids=set()
    )
    assert after_remove == []
    assert universal_access_service.list_activated_keys(db, caller=outsider) == []


def test_place_access_network_admin_approves(db):
    requester = _owner(
        db,
        email="req@example.com",
        zone_id="NET-A",
        account_type=AccountType.ENHANCED,
        role=OwnerRole.ADMINISTRATOR,
    )
    other = _owner(
        db,
        email="other@example.com",
        zone_id="NET-B",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    austin = _insert_zone(
        db,
        owner_id=other.id,
        zone_key="austin-hall",
        name="Austin hall",
    )
    db.execute(text("UPDATE zones SET zone_id = 'NET-B' WHERE id = :id"), {"id": austin})
    db.commit()

    created = universal_access_service.create_place_access_request(
        db,
        requester=requester,
        network_id="NET-B",
        zone_ids=[austin],
        photo_id=PHOTO,
        business_document=BUSINESS,
    )
    assert created["request_kind"] == "place_access"
    assert created["status"] == "pending"
    assert created["viewer_actions"]["can_edit_docs"] is True

    approved = universal_access_service.approve_request(
        db,
        caller=other,
        request_id=created["id"],
    )
    assert approved["status"] == "approved"
    assert approved["key_type"] == "private"
    assert approved["code"]
    private_keys = universal_access_service.list_private_keys(db, caller=requester)
    assert private_keys[0]["selected"] is True
    visible = universal_access_service.universal_primary_zones(db, requester, already_ids=set())
    assert [row.id for row in visible] == [austin]

    from app.services import zone_service

    listed = zone_service.list_zones(db, requester)
    assert any(int(row["id"]) == austin and row["config"].get("universal_access") for row in listed)

    admin_rows = universal_access_service.list_requests(db, caller=other)
    assert admin_rows[0]["viewer_actions"]["can_remove"] is True
    universal_access_service.delete_request(db, caller=other, request_id=created["id"])
    after_remove = universal_access_service.universal_primary_zones(
        db, requester, already_ids=set()
    )
    assert after_remove == []


def test_place_access_reject_and_edit_docs(db):
    requester = _owner(
        db,
        email="req@example.com",
        zone_id="NET-A",
        account_type=AccountType.ENHANCED,
        role=OwnerRole.USER,
    )
    requester.account_owner_id = requester.id
    other = _owner(
        db,
        email="other@example.com",
        zone_id="NET-B",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    zone = _insert_zone(db, owner_id=other.id, zone_key="hall", name="Hall")
    db.execute(text("UPDATE zones SET zone_id = 'NET-B' WHERE id = :id"), {"id": zone})
    db.commit()

    created = universal_access_service.create_place_access_request(
        db,
        requester=requester,
        network_id="NET-B",
        zone_ids=[zone],
        photo_id=PHOTO,
        business_document=BUSINESS,
    )
    new_photo = "data:image/jpeg;base64,abc"
    updated = universal_access_service.update_request_docs(
        db,
        caller=requester,
        request_id=created["id"],
        photo_id=new_photo,
        business_document=BUSINESS,
    )
    assert updated["photo_id"].startswith("data:image/jpeg")

    rejected = universal_access_service.reject_request(
        db,
        caller=other,
        request_id=created["id"],
        reason="Incomplete docs",
    )
    assert rejected["status"] == "rejected"
    assert "Incomplete" in rejected["reject_reason"]


def test_invitee_search_includes_other_networks(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.ENHANCED,
        role=OwnerRole.ADMINISTRATOR,
    )
    _owner(
        db,
        email="coworker@city.gov",
        zone_id="NET-A",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    _owner(
        db,
        email="worker@city.gov",
        zone_id="NET-OTHER",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    db.commit()

    matches = universal_access_service.search_invitees(db, admin=admin, query="city.gov")
    assert [row["email"] for row in matches] == ["worker@city.gov"]
    assert matches[0]["network_id"] == "NET-OTHER"


def test_admin_can_remove_generated_key(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    invitee = _owner(
        db,
        email="worker@example.com",
        zone_id="NET-B",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    invitee.account_owner_id = invitee.id
    shared = _insert_zone(db, owner_id=admin.id, zone_key="hall", name="Hall")
    db.execute(text("UPDATE zones SET zone_id = 'NET-A' WHERE id = :id"), {"id": shared})
    db.commit()

    created = universal_access_service.generate_invite_key(
        db,
        admin=admin,
        key_type="private",
        invitee_email=invitee.email,
        zone_ids=[shared],
    )
    universal_access_service.delete_request(db, caller=admin, request_id=created["id"])
    assert universal_access_service.list_requests(db, caller=invitee) == []
    assert universal_access_service.universal_primary_zones(db, invitee, already_ids=set()) == []


@pytest.mark.asyncio
async def test_approve_triggers_hub_open_notify(db):
    requester = _owner(
        db,
        email="req@example.com",
        zone_id="NET-A",
        account_type=AccountType.ENHANCED,
        role=OwnerRole.ADMINISTRATOR,
    )
    other = _owner(
        db,
        email="other@example.com",
        zone_id="NET-B",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    austin = _insert_zone(
        db,
        owner_id=other.id,
        zone_key="austin-hall",
        name="Austin hall",
    )
    db.execute(text("UPDATE zones SET zone_id = 'NET-B' WHERE id = :id"), {"id": austin})
    db.commit()

    created = universal_access_service.create_place_access_request(
        db,
        requester=requester,
        network_id="NET-B",
        zone_ids=[austin],
        photo_id=PHOTO,
        business_document=BUSINESS,
    )
    approved = universal_access_service.approve_request(
        db,
        caller=other,
        request_id=created["id"],
    )

    with patch(
        "app.services.smart_home_webhook_service.send_universal_access_open_webhooks",
        new_callable=AsyncMock,
    ) as send_open:
        send_open.return_value = {"ua_open_sent": 1}
        await universal_access_service.notify_hubs_of_zone_join(
            db,
            hub_owner_id=int(approved["network_admin_id"]),
            zone_ids=list(approved["zone_ids"]),
            member_id=int(approved["member_id"]),
            member_email=str(approved["email"]),
            member_name=str(approved["member_name"]),
        )

    send_open.assert_awaited_once()
    kwargs = send_open.await_args.kwargs
    assert kwargs["hub_owner_id"] == other.id
    assert kwargs["zone_record_ids"] == [austin]
    assert kwargs["member_email"] == "req@example.com"
    assert "Ada" in kwargs["member_name"]


@pytest.mark.asyncio
async def test_public_activate_triggers_hub_open_only_when_new(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    outsider = _owner(
        db,
        email="out@example.com",
        zone_id="NET-B",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    outsider.account_owner_id = outsider.id
    shared = _insert_zone(db, owner_id=admin.id, zone_key="hall", name="City hall")
    db.execute(text("UPDATE zones SET zone_id = 'NET-A' WHERE id = :id"), {"id": shared})
    db.commit()

    created = universal_access_service.generate_invite_key(
        db,
        admin=admin,
        key_type="public",
        zone_ids=[shared],
    )
    first = universal_access_service.activate_public_key(
        db, caller=outsider, code=created["code"]
    )
    assert first["newly_activated"] is True
    assert first["network_admin_id"] == admin.id

    with patch(
        "app.services.smart_home_webhook_service.send_universal_access_open_webhooks",
        new_callable=AsyncMock,
    ) as send_open:
        send_open.return_value = {"ua_open_sent": 1}
        await universal_access_service.notify_hubs_of_zone_join(
            db,
            hub_owner_id=int(first["network_admin_id"]),
            zone_ids=list(first["zone_ids"]),
            member_id=int(first["member_id"]),
            member_email=str(first["email"]),
            member_name=str(first["member_name"]),
        )
    send_open.assert_awaited_once()

    second = universal_access_service.activate_public_key(
        db, caller=outsider, code=created["code"]
    )
    assert second["newly_activated"] is False


def test_private_key_can_be_selected_and_cleared(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    invitee = _owner(
        db,
        email="worker@example.com",
        zone_id="NET-B",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    invitee.account_owner_id = invitee.id
    shared = _insert_zone(db, owner_id=admin.id, zone_key="hall", name="City hall")
    db.execute(text("UPDATE zones SET zone_id = 'NET-A' WHERE id = :id"), {"id": shared})
    db.commit()

    created = universal_access_service.generate_invite_key(
        db,
        admin=admin,
        key_type="private",
        invitee_email=invitee.email,
        zone_ids=[shared],
    )
    created = universal_access_service.approve_request(
        db, caller=invitee, request_id=created["id"]
    )
    listed = universal_access_service.list_private_keys(db, caller=invitee)
    assert listed[0]["selected"] is True
    assert [row.id for row in universal_access_service.universal_primary_zones(db, invitee, already_ids=set())] == [
        shared
    ]

    cleared = universal_access_service.set_private_key_selected(
        db,
        caller=invitee,
        credential_id=created["credential_id"],
        selected=False,
    )
    assert cleared["selected"] is False
    assert cleared["newly_selected"] is False
    assert universal_access_service.universal_primary_zones(db, invitee, already_ids=set()) == []
    assert universal_access_service.list_activated_keys(db, caller=invitee) == []

    chosen = universal_access_service.set_private_key_selected(
        db,
        caller=invitee,
        credential_id=created["credential_id"],
        selected=True,
    )
    assert chosen["newly_selected"] is True
    assert chosen["selected"] is True
    assert chosen["network_admin_id"] == admin.id
    assert chosen["email"] == "worker@example.com"
    assert [row.id for row in universal_access_service.universal_primary_zones(db, invitee, already_ids=set())] == [
        shared
    ]

    again = universal_access_service.set_private_key_selected(
        db,
        caller=invitee,
        credential_id=created["credential_id"],
        selected=True,
    )
    assert again["newly_selected"] is False


@pytest.mark.asyncio
async def test_private_select_triggers_hub_open_only_when_new(db):
    admin = _owner(
        db,
        email="net@example.com",
        zone_id="NET-A",
        account_type=AccountType.PRIVATE_PLUS,
        role=OwnerRole.ADMINISTRATOR,
    )
    invitee = _owner(
        db,
        email="worker@example.com",
        zone_id="NET-B",
        account_type=AccountType.EXCLUSIVE,
        role=OwnerRole.USER,
    )
    invitee.account_owner_id = invitee.id
    shared = _insert_zone(db, owner_id=admin.id, zone_key="hall", name="City hall")
    db.execute(text("UPDATE zones SET zone_id = 'NET-A' WHERE id = :id"), {"id": shared})
    db.commit()

    created = universal_access_service.generate_invite_key(
        db,
        admin=admin,
        key_type="private",
        invitee_email=invitee.email,
        zone_ids=[shared],
    )
    created = universal_access_service.approve_request(
        db, caller=invitee, request_id=created["id"]
    )
    universal_access_service.set_private_key_selected(
        db,
        caller=invitee,
        credential_id=created["credential_id"],
        selected=False,
    )
    chosen = universal_access_service.set_private_key_selected(
        db,
        caller=invitee,
        credential_id=created["credential_id"],
        selected=True,
    )
    assert chosen["newly_selected"] is True

    with patch(
        "app.services.smart_home_webhook_service.send_universal_access_open_webhooks",
        new_callable=AsyncMock,
    ) as send_open:
        send_open.return_value = {"ua_open_sent": 1}
        await universal_access_service.notify_hubs_of_zone_join(
            db,
            hub_owner_id=int(chosen["network_admin_id"]),
            zone_ids=list(chosen["zone_ids"]),
            member_id=int(chosen["member_id"]),
            member_email=str(chosen["email"]),
            member_name=str(chosen["member_name"]),
        )
    send_open.assert_awaited_once()
    kwargs = send_open.await_args.kwargs
    assert kwargs["hub_owner_id"] == admin.id
    assert kwargs["zone_record_ids"] == [shared]
    assert kwargs["member_email"] == "worker@example.com"
