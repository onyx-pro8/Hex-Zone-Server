"""Unit tests for smart-home outbound webhook delivery."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Device, Owner
from app.services.smart_home_webhook_service import (
    build_smart_home_webhook_payload,
    is_valid_webhook_url,
    message_origin_network_id,
    message_related_network_ids,
    normalize_webhook_url,
    owner_matches_message_network,
    owner_should_receive_webhook,
    send_smart_home_webhooks,
)


def _db_with_owner_and_hub(owner: SimpleNamespace, hid: str = "DEV-TEST1"):
    hub = SimpleNamespace(hid=hid, created_at=None)

    def query_side_effect(model):
        q = MagicMock()
        if model is Owner:
            q.filter.return_value.all.return_value = [owner]
        else:
            q.filter.return_value.order_by.return_value.all.return_value = [hub]
            q.filter.return_value.all.return_value = [hub]
        return q

    db = MagicMock()
    db.query.side_effect = query_side_effect
    return db


def test_normalize_webhook_url_adds_https():
    assert (
        normalize_webhook_url("webhook.site/e3ad20fa-abf3-42ed-8eda-e854ea66ed74")
        == "https://webhook.site/e3ad20fa-abf3-42ed-8eda-e854ea66ed74"
    )
    assert normalize_webhook_url("https://hub.example.com/x") == "https://hub.example.com/x"
    assert normalize_webhook_url("") == ""


def test_is_valid_webhook_url():
    assert is_valid_webhook_url("https://hub.example.com/hooks/hex") is True
    assert is_valid_webhook_url("http://192.168.1.10:8123/api/webhook/abc") is True
    assert (
        is_valid_webhook_url("webhook.site/e3ad20fa-abf3-42ed-8eda-e854ea66ed74")
        is True
    )
    assert is_valid_webhook_url("") is False
    assert is_valid_webhook_url("ftp://hub.example.com/x") is False
    assert is_valid_webhook_url("https://") is False
    assert is_valid_webhook_url("://missing-host") is False


def test_message_related_networks_include_fanout_not_only_sender():
    related = message_related_network_ids(
        {
            "zone_id": "DISTRICT-11",
            "metadata": {
                "sender_network_id": "INDIVIDUAL-SOLO",
                "fanout": {
                    "network_zone_id": "DISTRICT-11",
                    "matched_network_zone_ids": ["DISTRICT-11"],
                },
            },
        }
    )
    assert "DISTRICT-11" in related
    assert "INDIVIDUAL-SOLO" in related
    assert message_origin_network_id(
        {
            "metadata": {
                "sender_network_id": "INDIVIDUAL-SOLO",
                "fanout": {"network_zone_id": "DISTRICT-11"},
            }
        }
    ) == "DISTRICT-11"


def test_owner_matches_fanout_network_even_if_sender_differs():
    owner = SimpleNamespace(zone_id="DISTRICT-11")
    payload = {
        "metadata": {
            "sender_network_id": "INDIVIDUAL-SOLO",
            "fanout": {
                "network_zone_id": "DISTRICT-11",
                "matched_network_zone_ids": ["DISTRICT-11"],
            },
        }
    }
    assert owner_matches_message_network(owner, payload) is True
    assert (
        owner_matches_message_network(SimpleNamespace(zone_id="OTHER-NET"), payload)
        is False
    )


def test_build_smart_home_webhook_payload():
    body = build_smart_home_webhook_payload(
        {
            "id": 42,
            "type": "SENSOR",
            "category": "Alarm",
            "scope": "public",
            "priority": "MEDIUM",
            "text": "Door opened",
            "sender_id": 7,
            "zone_id": "ZONE-1",
            "created_at": "2026-01-01T00:00:00",
            "response_tracking_enabled": False,
            "metadata": {"hid": "DEV-A1B2C3", "position": {"latitude": 1.0}},
        },
        recipient_owner_id=9,
        network_id="ZONE-ABC",
    )
    assert body == {
        "title": "SENSOR in Safe Zone Patrol",
        "message": "Door opened",
    }
    assert "metadata" not in body
    assert "event" not in body


@pytest.mark.asyncio
async def test_send_smart_home_webhooks_posts_to_same_network_owners():
    owner = SimpleNamespace(
        id=9,
        active=True,
        sn_webhook="https://hub.example.com/hooks/hex",
        sn_hid="DEV-TEST1",
        zone_id="ZONE-ABC",
    )
    db = _db_with_owner_and_hub(owner)

    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "app.services.smart_home_webhook_service.httpx.AsyncClient",
        return_value=mock_client,
    ):
        stats = await send_smart_home_webhooks(
            db,
            [9],
            {
                "id": 1,
                "type": "PANIC",
                "category": "Alarm",
                "scope": "public",
                "priority": "MAX",
                "text": "Help",
                "sender_id": 2,
                "zone_id": "ZONE-1",
                "created_at": "2026-01-01T00:00:00",
                "metadata": {
                    "hid": "MOB-TEST",
                    "sender_network_id": "ZONE-ABC",
                },
            },
        )

    assert stats["webhook_sent"] == 1
    assert stats["webhook_failed"] == 0
    assert stats["webhook_owner_results"][0]["ok"] is True
    mock_client.post.assert_awaited_once()
    args, kwargs = mock_client.post.await_args
    assert args[0] == "https://hub.example.com/hooks/hex"
    assert kwargs["json"] == {
        "title": "PANIC in Safe Zone Patrol",
        "message": "Help",
    }


@pytest.mark.asyncio
async def test_send_webhook_when_delivered_on_admin_network_from_other_sender():
    """Individual sender + DISTRICT-11 delivery must notify admin hub."""
    owner = SimpleNamespace(
        id=1,
        active=True,
        sn_webhook="https://webhook.site/test-id",
        sn_hid="DEV-ADMIN",
        zone_id="DISTRICT-11",
    )
    db = _db_with_owner_and_hub(owner, hid="DEV-ADMIN")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "app.services.smart_home_webhook_service.httpx.AsyncClient",
        return_value=mock_client,
    ):
        stats = await send_smart_home_webhooks(
            db,
            [1, 3, 7],
            {
                "type": "PANIC",
                "text": "Help",
                "zone_id": "DISTRICT-11",
                "delivered_owner_ids": [1, 3, 7],
                "metadata": {
                    "sender_network_id": "INDIVIDUAL-SOLO",
                    "fanout": {
                        "network_zone_id": "DISTRICT-11",
                        "matched_network_zone_ids": ["DISTRICT-11"],
                    },
                },
            },
        )

    assert stats["webhook_sent"] == 1
    args, _kwargs = mock_client.post.await_args
    assert args[0] == "https://webhook.site/test-id"


def test_delivered_owner_always_gets_webhook_even_if_sender_network_differs():
    owner = SimpleNamespace(id=1, zone_id="DISTRICT-11")
    payload = {
        "delivered_owner_ids": [1, 3],
        "metadata": {
            "sender_network_id": "INDIVIDUAL-SOLO",
            "fanout": {
                "network_zone_id": "INDIVIDUAL-SOLO",
                "matched_network_zone_ids": ["INDIVIDUAL-SOLO"],
            },
        },
    }
    assert owner_should_receive_webhook(owner, payload) is True


@pytest.mark.asyncio
async def test_send_skips_without_registered_hub_hid():
    owner = SimpleNamespace(
        id=1,
        active=True,
        sn_webhook="https://webhook.site/test-id",
        sn_hid="",
        zone_id="DISTRICT-11",
    )

    def query_side_effect(model):
        q = MagicMock()
        if model is Owner:
            q.filter.return_value.all.return_value = [owner]
        else:
            q.filter.return_value.order_by.return_value.all.return_value = []
            q.filter.return_value.all.return_value = []
        return q

    db = MagicMock()
    db.query.side_effect = query_side_effect

    stats = await send_smart_home_webhooks(
        db,
        [1],
        {
            "type": "PANIC",
            "text": "Help",
            "delivered_owner_ids": [1],
            "metadata": {"sender_network_id": "DISTRICT-11"},
        },
    )
    assert stats["webhook_sent"] == 0
    assert stats.get("webhook_skipped_no_hid_count") == 1
    assert stats.get("webhook_owner_results") == []


@pytest.mark.asyncio
async def test_send_smart_home_webhooks_skips_other_networks():
    """Sender-only echo (not in delivered) is skipped when networks differ."""
    owner = SimpleNamespace(
        id=9,
        active=True,
        sn_webhook="https://hub.example.com/hooks/hex",
        sn_hid="DEV-TEST1",
        zone_id="ZONE-ABC",
    )
    db = _db_with_owner_and_hub(owner)

    stats = await send_smart_home_webhooks(
        db,
        [9],
        {
            "type": "PANIC",
            "text": "Help",
            "delivered_owner_ids": [42],
            "metadata": {
                "sender_network_id": "OTHER-NET",
                "fanout": {
                    "network_zone_id": "OTHER-NET",
                    "matched_network_zone_ids": ["OTHER-NET"],
                },
            },
        },
    )
    assert stats["webhook_sent"] == 0
    assert stats.get("webhook_skipped_network_count") == 1


@pytest.mark.asyncio
async def test_send_smart_home_webhooks_normalizes_scheme_less_url():
    owner = SimpleNamespace(
        id=9,
        active=True,
        sn_webhook="webhook.site/abc-def",
        sn_hid="DEV-TEST1",
        zone_id="ZONE-ABC",
    )
    db = _db_with_owner_and_hub(owner)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "app.services.smart_home_webhook_service.httpx.AsyncClient",
        return_value=mock_client,
    ):
        stats = await send_smart_home_webhooks(
            db,
            [9],
            {
                "type": "PANIC",
                "text": "Help",
                "metadata": {"sender_network_id": "ZONE-ABC"},
            },
        )

    assert stats["webhook_sent"] == 1
    args, _kwargs = mock_client.post.await_args
    assert args[0] == "https://webhook.site/abc-def"


@pytest.mark.asyncio
async def test_send_smart_home_webhooks_skips_without_url():
    owner = SimpleNamespace(
        id=9,
        active=True,
        sn_webhook="",
        sn_hid="DEV-TEST1",
        zone_id="ZONE-ABC",
    )
    db = _db_with_owner_and_hub(owner)

    stats = await send_smart_home_webhooks(
        db,
        [9],
        {
            "type": "SENSOR",
            "text": "x",
            "metadata": {"sender_network_id": "ZONE-ABC"},
        },
    )
    assert stats.get("webhook_no_urls") is True
    assert stats["webhook_sent"] == 0
