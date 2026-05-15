"""Tests for per-member message block rules."""
import pytest
from httpx import AsyncClient

from app.main import app
from app.services import message_block_service
from tests.test_main import _register_and_login, override_get_db, test_db


@pytest.mark.asyncio
async def test_block_message_type_hides_from_inbox(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        owner_a_id, token_a = await _register_and_login(
            client,
            email="block-type-a@example.com",
            zone_id="block-zone-1",
            first_name="Block",
            last_name="Alpha",
        )
        _owner_b_id, token_b = await _register_and_login(
            client,
            email="block-type-b@example.com",
            zone_id="block-zone-1",
            first_name="Block",
            last_name="Bravo",
        )

        post = await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"message": "Panic alert", "type": "PANIC"},
        )
        assert post.status_code == 201

        listed_before = await client.get(
            f"/messages?owner_id={owner_a_id}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert listed_before.status_code == 200
        assert any(m["message"] == "Panic alert" for m in listed_before.json())

        block = await client.post(
            "/message-feature/blocks",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"blocked_message_type": "PANIC"},
        )
        assert block.status_code == 201

        listed_after = await client.get(
            f"/messages?owner_id={owner_a_id}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert listed_after.status_code == 200
        assert not any(m["message"] == "Panic alert" for m in listed_after.json())


@pytest.mark.asyncio
async def test_block_member_hides_all_types(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        owner_a_id, token_a = await _register_and_login(
            client,
            email="block-member-a@example.com",
            zone_id="block-zone-2",
            first_name="Block",
            last_name="Alpha",
        )
        owner_b_id, token_b = await _register_and_login(
            client,
            email="block-member-b@example.com",
            zone_id="block-zone-2",
            first_name="Block",
            last_name="Bravo",
        )

        await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"message": "Service note", "type": "SERVICE"},
        )
        await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {token_b}"},
            json={
                "message": "Private note",
                "type": "PRIVATE",
                "receiver_id": owner_a_id,
            },
        )

        block = await client.post(
            "/message-feature/blocks",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"blocked_owner_id": owner_b_id},
        )
        assert block.status_code == 201

        listed = await client.get(
            f"/messages?owner_id={owner_a_id}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        texts = [m["message"] for m in listed.json()]
        assert "Service note" not in texts
        assert "Private note" not in texts


@pytest.mark.asyncio
async def test_private_message_rejected_when_recipient_blocked_sender(test_db, override_get_db):
    async with AsyncClient(app=app, base_url="http://test") as client:
        owner_a_id, token_a = await _register_and_login(
            client,
            email="block-send-a@example.com",
            zone_id="block-zone-3",
            first_name="Block",
            last_name="Alpha",
        )
        reg_b = await client.post(
            "/owners/register",
            json={
                "email": "block-send-b@example.com",
                "zone_id": "block-zone-3",
                "first_name": "Block",
                "last_name": "Bravo",
                "account_type": "private",
                "role": "user",
                "account_owner_id": owner_a_id,
                "password": "SecurePassword123",
                "registration_code": "FREE",
                "address": "Test Address 2",
            },
        )
        assert reg_b.status_code == 201
        login_b = await client.post(
            "/owners/login",
            json={"email": "block-send-b@example.com", "password": "SecurePassword123"},
        )
        assert login_b.status_code == 200
        owner_b_id = login_b.json()["owner_id"]
        token_b = login_b.json()["access_token"]

        block = await client.post(
            "/message-feature/blocks",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"blocked_owner_id": owner_b_id},
        )
        assert block.status_code == 201

        denied = await client.post(
            "/messages",
            headers={"Authorization": f"Bearer {token_b}"},
            json={
                "message": "Should not arrive",
                "type": "PRIVATE",
                "receiver_id": owner_a_id,
            },
        )
        assert denied.status_code == 403
        body = denied.json()
        detail = body.get("detail")
        if isinstance(detail, dict):
            assert detail.get("error_code") == "MESSAGE_BLOCKED_BY_RECIPIENT"
        else:
            assert "MESSAGE_BLOCKED_BY_RECIPIENT" in str(body)


def test_is_delivery_blocked_and_semantics(test_db):
    from app.models import MessageBlock

    block_type = MessageBlock(
        owner_id=1,
        blocked_owner_id=None,
        blocked_message_type="SENSOR",
    )
    block_member = MessageBlock(
        owner_id=1,
        blocked_owner_id=9,
        blocked_message_type=None,
    )
    block_both = MessageBlock(
        owner_id=1,
        blocked_owner_id=9,
        blocked_message_type="PANIC",
    )
    test_db.add_all([block_type, block_member, block_both])
    test_db.commit()

    assert message_block_service.is_delivery_blocked(
        test_db, recipient_owner_id=1, sender_owner_id=2, message_type="SENSOR"
    )
    assert not message_block_service.is_delivery_blocked(
        test_db, recipient_owner_id=1, sender_owner_id=2, message_type="SERVICE"
    )
    assert message_block_service.is_delivery_blocked(
        test_db, recipient_owner_id=1, sender_owner_id=9, message_type="CHAT"
    )
    assert message_block_service.is_delivery_blocked(
        test_db, recipient_owner_id=1, sender_owner_id=9, message_type="PANIC"
    )
    assert message_block_service.is_delivery_blocked(
        test_db, recipient_owner_id=1, sender_owner_id=None, message_type="SENSOR"
    )
    assert not message_block_service.is_delivery_blocked(
        test_db, recipient_owner_id=1, sender_owner_id=None, message_type="CHAT"
    )
