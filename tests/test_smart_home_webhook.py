"""Unit tests for smart-home outbound webhook delivery."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.smart_home_webhook_service import (
    build_smart_home_webhook_payload,
    is_valid_webhook_url,
    message_origin_network_id,
    owner_matches_message_network,
    send_smart_home_webhooks,
)


def test_is_valid_webhook_url():
    assert is_valid_webhook_url("https://hub.example.com/hooks/hex") is True
    assert is_valid_webhook_url("http://192.168.1.10:8123/api/webhook/abc") is True
    assert is_valid_webhook_url("") is False
    assert is_valid_webhook_url("ftp://hub.example.com/x") is False
    assert is_valid_webhook_url("not-a-url") is False
    assert is_valid_webhook_url("https://") is False


def test_message_origin_network_id_prefers_sender_network():
    assert (
        message_origin_network_id(
            {
                "metadata": {
                    "sender_network_id": "YUSIF",
                    "fanout": {"network_zone_id": "OTHER"},
                }
            }
        )
        == "YUSIF"
    )
    assert (
        message_origin_network_id(
            {"fanout": {"network_zone_id": "ZONE-ABC"}, "metadata": {}}
        )
        == "ZONE-ABC"
    )


def test_owner_matches_message_network():
    owner = SimpleNamespace(zone_id="YUSIF")
    assert owner_matches_message_network(
        owner, {"metadata": {"sender_network_id": "YUSIF"}}
    )
    assert not owner_matches_message_network(
        owner, {"metadata": {"sender_network_id": "OTHER"}}
    )
    assert not owner_matches_message_network(
        SimpleNamespace(zone_id=""),
        {"metadata": {"sender_network_id": "YUSIF"}},
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
    assert body["event"] == "SMART_HOME_ALARM"
    assert body["type"] == "SENSOR"
    assert body["hid"] == "DEV-A1B2C3"
    assert body["recipient_owner_id"] == 9
    assert body["network_id"] == "ZONE-ABC"
    assert body["text"] == "Door opened"
    assert body["title"] == "SENSOR (MEDIUM)"
    assert body["message"] == "Door opened"


@pytest.mark.asyncio
async def test_send_smart_home_webhooks_posts_to_same_network_owners():
    owner = SimpleNamespace(
        id=9,
        active=True,
        sn_webhook="https://hub.example.com/hooks/hex",
        zone_id="ZONE-ABC",
    )
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value.all.return_value = [owner]

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
    mock_client.post.assert_awaited_once()
    args, kwargs = mock_client.post.await_args
    assert args[0] == "https://hub.example.com/hooks/hex"
    assert kwargs["json"]["event"] == "SMART_HOME_ALARM"
    assert kwargs["json"]["type"] == "PANIC"


@pytest.mark.asyncio
async def test_send_smart_home_webhooks_skips_other_networks():
    owner = SimpleNamespace(
        id=9,
        active=True,
        sn_webhook="https://hub.example.com/hooks/hex",
        zone_id="ZONE-ABC",
    )
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value.all.return_value = [owner]

    stats = await send_smart_home_webhooks(
        db,
        [9],
        {
            "type": "PANIC",
            "text": "Help",
            "metadata": {"sender_network_id": "OTHER-NET"},
        },
    )
    assert stats["webhook_sent"] == 0
    assert stats.get("webhook_skipped_network_count") == 1


@pytest.mark.asyncio
async def test_send_smart_home_webhooks_skips_without_url():
    owner = SimpleNamespace(id=9, active=True, sn_webhook="", zone_id="ZONE-ABC")
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value.all.return_value = [owner]

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
