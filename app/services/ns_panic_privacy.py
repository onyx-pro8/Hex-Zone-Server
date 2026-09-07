"""NS-PANIC anti-retaliation: redact sender identity / GPS for clients.

Forensic fields remain in the DB and emergency log. Client-facing inbox,
WebSocket, and push payloads only expose message content (text + images)
plus a generic "Private" label for name and network id.
"""
from __future__ import annotations

import copy
from typing import Any

from app.domain.message_types import CanonicalMessageType, normalize_message_type

NS_PANIC_PUBLIC_LABEL = "Private"

# Content-only keys retained inside metadata.msg for NS_PANIC client payloads.
_NS_PANIC_MSG_CONTENT_KEYS = frozenset({"description", "title", "text", "images"})


def is_ns_panic_message_type(value: object) -> bool:
    try:
        return normalize_message_type(str(value or "")) == CanonicalMessageType.NS_PANIC
    except Exception:
        return False


def _sanitize_ns_panic_msg(msg: object) -> dict[str, Any]:
    if not isinstance(msg, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key in _NS_PANIC_MSG_CONTENT_KEYS:
        if key in msg:
            cleaned[key] = msg[key]
    return cleaned


def redact_ns_panic_geo_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return a client-safe copy of a geo-propagation result for NS_PANIC.

    Non-NS_PANIC payloads are returned unchanged (same object). Internal routing
    must use the original result's ``sender_id`` / ``delivered_owner_ids``.
    """
    if not isinstance(result, dict) or not is_ns_panic_message_type(result.get("type")):
        return result

    out = copy.deepcopy(result)
    out["sender_id"] = None
    out["receiver_id"] = None
    out["zone_id"] = NS_PANIC_PUBLIC_LABEL
    out["zone_ids"] = [NS_PANIC_PUBLIC_LABEL]
    out["broadcast_name"] = NS_PANIC_PUBLIC_LABEL
    out["delivered_owner_ids"] = []
    out["blocked_owner_ids"] = []
    out["fanout"] = {"redacted": True}

    meta = out.get("metadata")
    if isinstance(meta, dict):
        for key in (
            "position",
            "city",
            "province",
            "country",
            "sender_relevant_zone",
            "recipient_relevant_zones",
            "zone_ids",
            "delivered_owner_ids",
            "blocked_owner_ids",
            "fanout",
            "hid",
            "tt",
            "to",
            "co",
            "broadcast_name",
            "broadcastName",
        ):
            meta.pop(key, None)
        meta["msg"] = _sanitize_ns_panic_msg(meta.get("msg"))
        meta["identity_redacted"] = True
        out["metadata"] = meta

    return out


def apply_ns_panic_redaction_to_zone_message_fields(
    *,
    message_type: object,
    zone_id: str,
    sender_id: int | None,
    broadcast_name: str | None,
    latitude: float | None,
    longitude: float | None,
    delivered_owner_ids: list[int] | None,
    relevant_zone_fields: dict[str, str | None],
) -> dict[str, Any]:
    """Field overrides for ``ZoneMessageResponse`` construction."""
    if not is_ns_panic_message_type(message_type):
        return {
            "zone_id": zone_id,
            "sender_id": sender_id,
            "broadcast_name": broadcast_name,
            "latitude": latitude,
            "longitude": longitude,
            "delivered_owner_ids": delivered_owner_ids,
            **relevant_zone_fields,
        }
    return {
        "zone_id": NS_PANIC_PUBLIC_LABEL,
        "sender_id": None,
        "broadcast_name": NS_PANIC_PUBLIC_LABEL,
        "latitude": None,
        "longitude": None,
        "delivered_owner_ids": None,
        "relevant_zone_name": None,
        "relevant_zone_network_id": None,
        "relevant_zone_label": NS_PANIC_PUBLIC_LABEL,
    }
