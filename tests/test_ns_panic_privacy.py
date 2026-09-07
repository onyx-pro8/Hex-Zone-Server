"""NS-PANIC client payload redaction (anti-retaliation)."""

from app.services.ns_panic_privacy import (
    NS_PANIC_PUBLIC_LABEL,
    apply_ns_panic_redaction_to_zone_message_fields,
    redact_ns_panic_geo_result,
)


def test_redact_ns_panic_geo_result_strips_identity_and_gps():
    raw = {
        "id": "evt-1",
        "sender_id": 42,
        "receiver_id": None,
        "zone_id": "DISTRICT-11",
        "zone_ids": ["DISTRICT-11"],
        "type": "NS_PANIC",
        "category": "Alarm",
        "scope": "public",
        "text": "Emergency!!!",
        "delivered_owner_ids": [7, 8, 9],
        "blocked_owner_ids": [],
        "created_at": "2026-08-19T13:52:08",
        "metadata": {
            "msg": {
                "description": "Emergency!!!",
                "broadcast_name": "ME",
                "images": ["https://cdn.example/a.jpg"],
            },
            "position": {"latitude": 49.6511, "longitude": 23.854},
            "zone_ids": ["DISTRICT-11"],
            "delivered_owner_ids": [7, 8, 9],
            "fanout": {"strategy": "primary_zone"},
            "sender_relevant_zone": {"name": "Home", "network_id": "DISTRICT-11"},
        },
        "fanout": {"strategy": "primary_zone"},
    }

    redacted = redact_ns_panic_geo_result(raw)

    assert redacted is not raw
    assert redacted["sender_id"] is None
    assert redacted["zone_id"] == NS_PANIC_PUBLIC_LABEL
    assert redacted["zone_ids"] == [NS_PANIC_PUBLIC_LABEL]
    assert redacted["broadcast_name"] == NS_PANIC_PUBLIC_LABEL
    assert redacted["delivered_owner_ids"] == []
    assert redacted["text"] == "Emergency!!!"
    meta = redacted["metadata"]
    assert "position" not in meta
    assert "fanout" not in meta
    assert "sender_relevant_zone" not in meta
    assert meta["msg"] == {
        "description": "Emergency!!!",
        "images": ["https://cdn.example/a.jpg"],
    }
    # Original untouched for routing.
    assert raw["sender_id"] == 42
    assert raw["metadata"]["position"]["latitude"] == 49.6511


def test_redact_leaves_panic_unchanged():
    raw = {
        "type": "PANIC",
        "sender_id": 42,
        "zone_id": "DISTRICT-11",
        "metadata": {"position": {"latitude": 1.0, "longitude": 2.0}},
    }
    assert redact_ns_panic_geo_result(raw) is raw


def test_zone_message_field_redaction():
    fields = apply_ns_panic_redaction_to_zone_message_fields(
        message_type="NS-PANIC",
        zone_id="DISTRICT-11",
        sender_id=42,
        broadcast_name="Alice",
        latitude=49.0,
        longitude=23.0,
        delivered_owner_ids=[1, 2],
        relevant_zone_fields={
            "relevant_zone_name": "Home",
            "relevant_zone_network_id": "DISTRICT-11",
            "relevant_zone_label": "Home (DISTRICT-11)",
        },
    )
    assert fields["zone_id"] == NS_PANIC_PUBLIC_LABEL
    assert fields["sender_id"] is None
    assert fields["broadcast_name"] == NS_PANIC_PUBLIC_LABEL
    assert fields["latitude"] is None
    assert fields["longitude"] is None
    assert fields["delivered_owner_ids"] is None
    assert fields["relevant_zone_label"] == NS_PANIC_PUBLIC_LABEL
