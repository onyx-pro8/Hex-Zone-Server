"""Unit tests for smart-home outbound webhook delivery."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Device, Owner, Zone
from app.services.smart_home_webhook_service import (
    build_smart_home_webhook_payload,
    build_universal_access_open_payload,
    device_matches_message_zones,
    is_valid_webhook_url,
    message_origin_network_id,
    message_related_network_ids,
    normalize_webhook_url,
    owner_matches_message_network,
    owner_should_receive_webhook,
    send_smart_home_webhooks,
    send_universal_access_open_webhooks,
)


def _hub(owner: SimpleNamespace, hid: str, *, zone_ids: list[int] | None = None, webhook: str | None = None):
    return SimpleNamespace(
        hid=hid,
        created_at=None,
        active=True,
        webhook=owner.sn_webhook if webhook is None else webhook,
        zone_ids=list(zone_ids if zone_ids is not None else getattr(owner, "linked_zone_ids", None) or [1]),
    )


def _zone(zone_id: int, network_id: str):
    return SimpleNamespace(id=zone_id, zone_id=network_id, name=f"Zone {zone_id}")


def _db_with_owner_and_hub(
    owner: SimpleNamespace,
    hid: str = "DEV-TEST1",
    *,
    sender: SimpleNamespace | None = None,
    hubs: list | None = None,
    zones: list | None = None,
):
    hub_rows = hubs if hubs is not None else [_hub(owner, hid)]
    zone_rows = list(zones or [])

    def query_side_effect(*models):
        q = MagicMock()
        model = models[0] if models else None
        if model is Owner:
            q.filter.return_value.all.return_value = [owner]
            q.filter.return_value.first.return_value = sender
        elif model is Device:
            q.filter.return_value.order_by.return_value.all.return_value = hub_rows
            q.filter.return_value.all.return_value = hub_rows
        elif model is Zone:
            q.filter.return_value.all.return_value = zone_rows
        else:
            q.filter.return_value.all.return_value = []
        return q

    db = MagicMock()
    db.query.side_effect = query_side_effect
    zone_by_id = {int(row.id): row for row in zone_rows}

    def get_side_effect(model, pk):
        if model is Zone:
            try:
                return zone_by_id.get(int(pk))
            except (TypeError, ValueError):
                return None
        return None

    db.get.side_effect = get_side_effect
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
    sender = SimpleNamespace(
        id=7,
        message_display_name="Alex Home",
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = sender

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
            "metadata": {
                "hid": "DEV-A1B2C3",
                "position": {"latitude": 43.6532, "longitude": -79.3832},
                "sender_relevant_zone": {"name": "Front Gate"},
                "recipient_relevant_zones": {
                    "9": {"name": "North Yard"},
                },
            },
        },
        recipient_owner_id=9,
        network_id="ZONE-ABC",
        db=db,
    )
    assert body == {
        "title": "Alex Home · North Yard",
        "message": "SENSOR: Door opened\nLocation: 43.6532, -79.3832",
    }
    assert "metadata" not in body
    assert "event" not in body


def test_build_smart_home_webhook_payload_fallbacks():
    body = build_smart_home_webhook_payload(
        {
            "type": "PANIC",
            "text": "Help",
            "sender_id": None,
            "metadata": {"position": {"latitude": 1.0}},
        },
        recipient_owner_id=9,
    )
    assert body == {
        "title": "Guest · Unknown zone",
        "message": "PANIC: Help\nLocation: unknown",
    }


def test_build_smart_home_webhook_payload_uses_inbox_zone_label():
    """Hub title matches the inbox bubble heading (name + sender network id)."""
    sender = SimpleNamespace(id=7, message_display_name="Gary Peart")
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = sender

    body = build_smart_home_webhook_payload(
        {
            "type": "SENSOR",
            "text": "Sensor message",
            "sender_id": 7,
            "metadata": {
                "sender_network_id": "PEART",
                "position": {"latitude": 43.73114, "longitude": -79.52283},
                "recipient_relevant_zones": {
                    "9": {
                        "name": "Oklahoma Drive Neighbourhood",
                        "network_id": "PEART",
                        "sender_network_id": "PEART",
                        "label": "Oklahoma Drive Neighbourhood (PEART)",
                    },
                },
            },
        },
        recipient_owner_id=9,
        db=db,
    )
    assert body["title"] == "Gary Peart · Oklahoma Drive Neighbourhood (PEART)"
    assert "Location: 43.73114, -79.52283" in body["message"]


def test_build_smart_home_webhook_payload_label_without_name_field():
    """Prefer embedded label when name is missing (same string the bubble shows)."""
    body = build_smart_home_webhook_payload(
        {
            "type": "SENSOR",
            "text": "Sensor message",
            "sender_id": None,
            "metadata": {
                "sender_relevant_zone": {
                    "label": "Oklahoma Drive Neighbourhood (PEART)",
                },
                "position": {"latitude": 1.0, "longitude": 2.0},
            },
        },
        recipient_owner_id=9,
    )
    assert body["title"] == "Guest · Oklahoma Drive Neighbourhood (PEART)"

@pytest.mark.asyncio
async def test_send_smart_home_webhooks_posts_to_same_network_owners():
    owner = SimpleNamespace(
        id=9,
        active=True,
        sn_webhook="https://hub.example.com/hooks/hex",
        sn_hid="DEV-TEST1",
        zone_id="ZONE-ABC",
    )
    sender = SimpleNamespace(id=2, message_display_name="Pat")
    db = _db_with_owner_and_hub(owner, sender=sender)

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
                    "position": {"latitude": 10.5, "longitude": 20.25},
                    "sender_relevant_zone": {"name": "Lobby"},
                    "recipient_zone_record_ids": {"9": 1},
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
        "title": "Pat · Lobby (ZONE-ABC)",
        "message": "PANIC: Help\nLocation: 10.5, 20.25",
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
                    "recipient_zone_record_ids": {"1": 1},
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
                "metadata": {
                    "sender_network_id": "ZONE-ABC",
                    "recipient_zone_record_ids": {"9": 1},
                },
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


@pytest.mark.asyncio
async def test_send_only_the_hub_linked_to_the_message_zone():
    owner = SimpleNamespace(
        id=9,
        active=True,
        sn_webhook="",
        sn_hid="",
        zone_id="ZONE-ABC",
    )
    front = _hub(owner, "DEV-FRONT", zone_ids=[11], webhook="https://hub.example/front")
    back = _hub(owner, "DEV-BACK", zone_ids=[22], webhook="https://hub.example/back")
    db = _db_with_owner_and_hub(owner, hubs=[front, back])

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
                "type": "SENSOR",
                "text": "Door",
                "delivered_owner_ids": [9],
                "metadata": {
                    "sender_network_id": "ZONE-ABC",
                    "recipient_zone_record_ids": {"9": 22},
                },
            },
        )

    assert stats["webhook_sent"] == 1
    assert stats["webhook_skipped_no_zone_count"] == 1
    args, _kwargs = mock_client.post.await_args
    assert args[0] == "https://hub.example/back"


def test_zone_message_requires_linked_zone_not_only_network():
    """SENSOR with zone records ignores a hub that only shares the network."""
    owner = SimpleNamespace(id=9, zone_id="ZONE-ABC", sn_webhook="")
    hub = _hub(owner, "DEV-FRONT", zone_ids=[11])
    db = _db_with_owner_and_hub(
        owner,
        hubs=[hub],
        zones=[_zone(11, "ZONE-ABC")],
    )
    payload = {
        "type": "SENSOR",
        "delivered_owner_ids": [9],
        "metadata": {
            "sender_network_id": "ZONE-ABC",
            "recipient_zone_record_ids": {"9": 22},
        },
    }
    assert device_matches_message_zones(db, hub, 9, payload) is False


def test_unknown_matches_any_hub_on_message_network():
    """UNKNOWN notifies every hub linked to a zone on the event network."""
    owner = SimpleNamespace(id=9, zone_id="ZONE-ABC", sn_webhook="")
    front = _hub(owner, "DEV-FRONT", zone_ids=[11])
    back = _hub(owner, "DEV-BACK", zone_ids=[22])
    other = _hub(owner, "DEV-OTHER", zone_ids=[33])
    db = _db_with_owner_and_hub(
        owner,
        hubs=[front, back, other],
        zones=[
            _zone(11, "ZONE-ABC"),
            _zone(22, "ZONE-ABC"),
            _zone(33, "OTHER-NET"),
        ],
    )
    payload = {
        "type": "UNKNOWN",
        "delivered_owner_ids": [9],
        "metadata": {
            "sender_network_id": "ZONE-ABC",
            "fanout": {"strategy": "unknown_nearest_global"},
        },
    }
    assert device_matches_message_zones(db, front, 9, payload) is True
    assert device_matches_message_zones(db, back, 9, payload) is True
    assert device_matches_message_zones(db, other, 9, payload) is False


@pytest.mark.asyncio
async def test_unknown_posts_to_all_hubs_on_message_network():
    owner = SimpleNamespace(
        id=9,
        active=True,
        sn_webhook="",
        sn_hid="",
        zone_id="ZONE-ABC",
    )
    front = _hub(owner, "DEV-FRONT", zone_ids=[11], webhook="https://hub.example/front")
    back = _hub(owner, "DEV-BACK", zone_ids=[22], webhook="https://hub.example/back")
    other = _hub(owner, "DEV-OTHER", zone_ids=[33], webhook="https://hub.example/other")
    db = _db_with_owner_and_hub(
        owner,
        hubs=[front, back, other],
        zones=[
            _zone(11, "ZONE-ABC"),
            _zone(22, "ZONE-ABC"),
            _zone(33, "OTHER-NET"),
        ],
    )

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
                "type": "UNKNOWN",
                "text": "Something nearby",
                "delivered_owner_ids": [9],
                "metadata": {
                    "sender_network_id": "ZONE-ABC",
                    "position": {"latitude": 1.0, "longitude": 2.0},
                    "fanout": {"strategy": "unknown_nearest_global"},
                },
            },
        )

    assert stats["webhook_sent"] == 2
    assert stats["webhook_skipped_no_zone_count"] == 1
    posted = sorted(call.args[0] for call in mock_client.post.await_args_list)
    assert posted == ["https://hub.example/back", "https://hub.example/front"]


def test_build_universal_access_open_payload():
    assert build_universal_access_open_payload(
        zone_id="NET-B",
        email="req@example.com",
        name="Ada Lovelace",
    ) == {
        "zone_id": "NET-B",
        "email": "req@example.com",
        "name": "Ada Lovelace",
        "action": "open",
    }


@pytest.mark.asyncio
async def test_ua_open_posts_only_to_hubs_linked_to_granted_zones():
    owner = SimpleNamespace(id=3, active=True, sn_webhook="", sn_hid="", zone_id="NET-B")
    front = _hub(owner, "DEV-FRONT", zone_ids=[11], webhook="https://hub.example/front")
    back = _hub(owner, "DEV-BACK", zone_ids=[22], webhook="https://hub.example/back")
    db = _db_with_owner_and_hub(
        owner,
        hubs=[front, back],
        zones=[_zone(11, "NET-B"), _zone(22, "NET-B")],
    )

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
        stats = await send_universal_access_open_webhooks(
            db,
            hub_owner_id=3,
            zone_record_ids=[22],
            member_email="req@example.com",
            member_name="Ada Lovelace",
        )

    assert stats["ua_open_sent"] == 1
    assert stats["ua_open_failed"] == 0
    mock_client.post.assert_awaited_once()
    args, kwargs = mock_client.post.await_args
    assert args[0] == "https://hub.example/back"
    assert kwargs["json"] == {
        "zone_id": "NET-B",
        "email": "req@example.com",
        "name": "Ada Lovelace",
        "action": "open",
    }


@pytest.mark.asyncio
async def test_ua_open_posts_to_each_granted_zone_hub():
    owner = SimpleNamespace(id=3, active=True, sn_webhook="", sn_hid="", zone_id="NET-B")
    front = _hub(owner, "DEV-FRONT", zone_ids=[11], webhook="https://hub.example/front")
    back = _hub(owner, "DEV-BACK", zone_ids=[22], webhook="https://hub.example/back")
    db = _db_with_owner_and_hub(
        owner,
        hubs=[front, back],
        zones=[_zone(11, "HALL-A"), _zone(22, "HALL-B")],
    )

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
        stats = await send_universal_access_open_webhooks(
            db,
            hub_owner_id=3,
            zone_record_ids=[11, 22],
            member_email="out@example.com",
            member_name="Out User",
        )

    assert stats["ua_open_sent"] == 2
    bodies = [call.kwargs["json"] for call in mock_client.post.await_args_list]
    assert {
        "zone_id": "HALL-A",
        "email": "out@example.com",
        "name": "Out User",
        "action": "open",
    } in bodies
    assert {
        "zone_id": "HALL-B",
        "email": "out@example.com",
        "name": "Out User",
        "action": "open",
    } in bodies
