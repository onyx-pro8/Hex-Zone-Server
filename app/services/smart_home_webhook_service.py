"""Outbound smart-home webhook delivery for geo-propagated alarms/alerts.

When an owner configures ``owners.sn_webhook``, Hex Zone POSTs each delivered
geo message to that URL so a hub can sound/show the alarm without polling.

Hubs only receive alarms that originated on the hub owner's network
(``owners.zone_id`` must match the message's sender network). Failures never
fail the originating request.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.message_types import is_pushable_geo_type
from app.models import Owner

logger = logging.getLogger(__name__)


def _webhook_timeout_seconds() -> float:
    return max(1.0, float(getattr(settings, "SMART_HOME_WEBHOOK_TIMEOUT_SECONDS", 5)))


def normalize_webhook_url(url: str) -> str:
    """Trim and add https:// when the user omitted the scheme (common paste)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if len(raw) > 2048:
        return raw
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    return raw


def is_valid_webhook_url(url: str) -> bool:
    """Accept only absolute http(s) URLs (after optional scheme normalization)."""
    raw = normalize_webhook_url(url)
    if not raw or len(raw) > 2048:
        return False
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    return True


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def message_origin_network_id(alarm_payload: dict[str, Any]) -> str:
    """Network id the geo message was made on (sender's account network)."""
    metadata = _as_dict(alarm_payload.get("metadata"))
    sender_net = str(metadata.get("sender_network_id") or "").strip()
    if sender_net:
        return sender_net

    meta_network = str(metadata.get("network_zone_id") or "").strip()
    if meta_network:
        return meta_network

    fanout = _as_dict(metadata.get("fanout")) or _as_dict(alarm_payload.get("fanout"))
    network_zone = str(fanout.get("network_zone_id") or "").strip()
    if network_zone:
        return network_zone

    return ""


def owner_matches_message_network(owner: Owner, alarm_payload: dict[str, Any]) -> bool:
    """True when the hub owner's network is the message's origin network."""
    owner_net = str(getattr(owner, "zone_id", "") or "").strip()
    if not owner_net:
        return False
    origin = message_origin_network_id(alarm_payload)
    if not origin:
        return False
    return owner_net.casefold() == origin.casefold()


def build_smart_home_webhook_payload(
    alarm_payload: dict[str, Any],
    *,
    recipient_owner_id: int,
    network_id: str | None = None,
) -> dict[str, Any]:
    """Stable JSON body hubs can parse for sirens / notifications.

    Includes Hex Zone fields (``type``, ``text``, …) plus Home Assistant–friendly
    ``title`` / ``message`` aliases for client automations.
    """
    metadata = _as_dict(alarm_payload.get("metadata"))
    hid = str(metadata.get("hid") or "").strip()
    msg_type = str(alarm_payload.get("type") or "").strip().upper()
    priority = str(alarm_payload.get("priority") or "").strip()
    text = str(alarm_payload.get("text") or "")
    title = msg_type or "System Update"
    if priority:
        title = f"{title} ({priority})"
    return {
        "event": "SMART_HOME_ALARM",
        "id": alarm_payload.get("id"),
        "type": msg_type,
        "category": str(alarm_payload.get("category") or ""),
        "scope": str(alarm_payload.get("scope") or ""),
        "priority": priority,
        "text": text,
        # Client Home Assistant automations expect these keys.
        "title": title,
        "message": text,
        "sender_id": alarm_payload.get("sender_id"),
        "zone_id": alarm_payload.get("zone_id"),
        "network_id": (network_id or "").strip(),
        "recipient_owner_id": recipient_owner_id,
        "hid": hid,
        "created_at": alarm_payload.get("created_at"),
        "response_tracking_enabled": bool(
            alarm_payload.get("response_tracking_enabled")
        ),
        "metadata": metadata,
    }


async def _post_webhook(
    client: httpx.AsyncClient,
    *,
    url: str,
    body: dict[str, Any],
) -> bool:
    try:
        response = await client.post(url, json=body)
        if response.status_code >= 400:
            logger.warning(
                "Smart-home webhook HTTP %s for %s (type=%s id=%s)",
                response.status_code,
                url,
                body.get("type"),
                body.get("id"),
            )
            return False
        return True
    except httpx.HTTPError as exc:
        logger.warning(
            "Smart-home webhook failed for %s (type=%s id=%s): %s",
            url,
            body.get("type"),
            body.get("id"),
            exc,
        )
        return False


async def send_smart_home_webhooks(
    db: Session,
    owner_ids: list[int],
    alarm_payload: dict[str, Any],
) -> dict[str, Any]:
    """POST the alarm to each same-network recipient owner's webhook URL.

    Only owners whose ``zone_id`` matches the message origin network are
    notified. Returns counts for response diagnostics. Never raises.
    """
    msg_type = str(alarm_payload.get("type") or "")
    if not is_pushable_geo_type(msg_type):
        return {"webhook_sent": 0, "webhook_failed": 0, "webhook_skipped": True}

    unique_ids = sorted({int(oid) for oid in owner_ids if isinstance(oid, int)})
    if not unique_ids:
        return {"webhook_sent": 0, "webhook_failed": 0, "webhook_no_targets": True}

    origin_network = message_origin_network_id(alarm_payload)
    if not origin_network:
        logger.info(
            "Smart-home webhook type=%s skipped: missing origin network",
            msg_type,
        )
        return {
            "webhook_sent": 0,
            "webhook_failed": 0,
            "webhook_skipped_network": True,
        }

    owners = (
        db.query(Owner)
        .filter(Owner.id.in_(unique_ids), Owner.active.is_(True))
        .all()
    )
    targets: list[tuple[Owner, str]] = []
    skipped_network = 0
    for owner in owners:
        if not owner_matches_message_network(owner, alarm_payload):
            skipped_network += 1
            continue
        url = normalize_webhook_url(str(getattr(owner, "sn_webhook", "") or ""))
        if not url:
            continue
        if not is_valid_webhook_url(url):
            logger.warning(
                "Ignoring invalid smart-home webhook for owner %s: %s",
                owner.id,
                url[:80],
            )
            continue
        targets.append((owner, url))

    if not targets:
        return {
            "webhook_sent": 0,
            "webhook_failed": 0,
            "webhook_no_urls": True,
            "webhook_skipped_network_count": skipped_network,
        }

    sent = 0
    failed = 0
    timeout = _webhook_timeout_seconds()
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "HexZone-SmartHomeWebhook/1.0",
            "X-Hex-Zone-Event": "SMART_HOME_ALARM",
        },
    ) as client:
        for owner, url in targets:
            body = build_smart_home_webhook_payload(
                alarm_payload,
                recipient_owner_id=owner.id,
                network_id=str(owner.zone_id or ""),
            )
            ok = await _post_webhook(client, url=url, body=body)
            if ok:
                sent += 1
            else:
                failed += 1

    logger.info(
        "Smart-home webhook type=%s origin=%s targets=%d sent=%d failed=%d skipped_network=%d",
        msg_type,
        origin_network,
        len(targets),
        sent,
        failed,
        skipped_network,
    )
    return {
        "webhook_sent": sent,
        "webhook_failed": failed,
        "webhook_targets": len(targets),
        "webhook_skipped_network_count": skipped_network,
    }
