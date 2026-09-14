"""Outbound smart-home webhook delivery for geo-propagated alarms/alerts.

When an owner configures ``owners.sn_webhook``, Hex Zone POSTs each delivered
geo message to that URL so a hub can sound/show the alarm without polling.

Hubs are notified for owners in the delivery set whose account network appears
in the message's routing networks (fanout matched networks and/or sender
network). Failures never fail the originating request.
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


def _add_network(bucket: set[str], value: Any) -> None:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            bucket.add(cleaned)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _add_network(bucket, item)


def message_related_network_ids(alarm_payload: dict[str, Any]) -> set[str]:
    """Networks this geo message was routed on (fanout + sender)."""
    networks: set[str] = set()
    metadata = _as_dict(alarm_payload.get("metadata"))
    fanout = _as_dict(metadata.get("fanout")) or _as_dict(alarm_payload.get("fanout"))

    _add_network(networks, metadata.get("sender_network_id"))
    _add_network(networks, metadata.get("network_zone_id"))
    _add_network(networks, fanout.get("network_zone_id"))
    _add_network(networks, fanout.get("matched_network_zone_ids"))
    _add_network(networks, alarm_payload.get("zone_ids"))
    # Single zone_id on the event is often the primary matched network id.
    _add_network(networks, alarm_payload.get("zone_id"))
    return networks


def message_origin_network_id(alarm_payload: dict[str, Any]) -> str:
    """Primary network id for diagnostics / payload display."""
    metadata = _as_dict(alarm_payload.get("metadata"))
    fanout = _as_dict(metadata.get("fanout")) or _as_dict(alarm_payload.get("fanout"))

    for candidate in (
        fanout.get("network_zone_id"),
        metadata.get("network_zone_id"),
        metadata.get("sender_network_id"),
        alarm_payload.get("zone_id"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    matched = fanout.get("matched_network_zone_ids")
    if isinstance(matched, list):
        for item in matched:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def owner_matches_message_network(owner: Owner, alarm_payload: dict[str, Any]) -> bool:
    """True when the hub owner's network is one of the message routing networks.

    If routing networks cannot be determined, allow the owner (geo delivery
    already decided they should receive the alarm).
    """
    owner_net = str(getattr(owner, "zone_id", "") or "").strip()
    if not owner_net:
        return False
    related = message_related_network_ids(alarm_payload)
    if not related:
        return True
    owner_key = owner_net.casefold()
    return any(owner_key == net.casefold() for net in related)


def _delivered_owner_id_set(alarm_payload: dict[str, Any]) -> set[int]:
    raw = alarm_payload.get("delivered_owner_ids")
    if not isinstance(raw, list):
        metadata = _as_dict(alarm_payload.get("metadata"))
        raw = metadata.get("delivered_owner_ids")
    if not isinstance(raw, list):
        return set()
    out: set[int] = set()
    for item in raw:
        try:
            out.add(int(item))
        except (TypeError, ValueError):
            continue
    return out


def owner_should_receive_webhook(owner: Owner, alarm_payload: dict[str, Any]) -> bool:
    """Delivered recipients always qualify; sender-only echo uses network match.

    If ``delivered_owner_ids`` is missing/empty (e.g. NS_PANIC client redaction),
    trust the caller-scoped ``owner_ids`` list and allow the owner.
    """
    delivered = _delivered_owner_id_set(alarm_payload)
    try:
        owner_id = int(owner.id)
    except (TypeError, ValueError):
        return False
    if not delivered:
        return True
    if owner_id in delivered:
        return True
    return owner_matches_message_network(owner, alarm_payload)


def build_smart_home_webhook_payload(
    alarm_payload: dict[str, Any],
    *,
    recipient_owner_id: int,
    network_id: str | None = None,
) -> dict[str, Any]:
    """JSON body for client Home Assistant webhooks.

    Contract (client automation)::

        title   -> trigger.json.title   (default \"System Update\")
        message -> trigger.json.message

    Extra Hex Zone diagnostics are omitted so hubs only see the HA fields.
    ``recipient_owner_id`` / ``network_id`` are accepted for call-site
    compatibility but not included in the POST body.
    """
    del recipient_owner_id, network_id  # reserved for future hub routing
    msg_type = str(alarm_payload.get("type") or "").strip().upper()
    text = str(alarm_payload.get("text") or "")
    if not text:
        metadata = _as_dict(alarm_payload.get("metadata"))
        msg = _as_dict(metadata.get("msg"))
        text = str(
            msg.get("description")
            or msg.get("title")
            or msg.get("text")
            or ""
        )
    # Client HA title format, e.g. "PANIC in Safe Zone Patrol".
    title = f"{msg_type} in Safe Zone Patrol" if msg_type else "System Update"
    return {
        "title": title,
        "message": text,
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
                "Smart-home webhook HTTP %s for %s (title=%s)",
                response.status_code,
                url,
                body.get("title"),
            )
            return False
        return True
    except httpx.HTTPError as exc:
        logger.warning(
            "Smart-home webhook failed for %s (title=%s): %s",
            url,
            body.get("title"),
            exc,
        )
        return False


async def send_smart_home_webhooks(
    db: Session,
    owner_ids: list[int],
    alarm_payload: dict[str, Any],
) -> dict[str, Any]:
    """POST the alarm to each eligible recipient owner's webhook URL.

    Eligibility: owner is in ``owner_ids``, has a valid ``sn_webhook``, and their
    ``zone_id`` matches a routing network for this message (or routing metadata
    is missing). Returns counts for response diagnostics. Never raises.
    """
    msg_type = str(alarm_payload.get("type") or "")
    if not is_pushable_geo_type(msg_type):
        logger.info("Smart-home webhook type=%s skipped: not a pushable geo type", msg_type)
        return {"webhook_sent": 0, "webhook_failed": 0, "webhook_skipped": True}

    unique_ids = sorted({int(oid) for oid in owner_ids if isinstance(oid, int)})
    if not unique_ids:
        logger.info("Smart-home webhook type=%s skipped: no owner targets", msg_type)
        return {"webhook_sent": 0, "webhook_failed": 0, "webhook_no_targets": True}

    related_networks = message_related_network_ids(alarm_payload)
    origin_network = message_origin_network_id(alarm_payload)

    owners = (
        db.query(Owner)
        .filter(Owner.id.in_(unique_ids), Owner.active.is_(True))
        .all()
    )
    targets: list[tuple[Owner, str]] = []
    skipped_network = 0
    skipped_no_url = 0
    for owner in owners:
        if not owner_should_receive_webhook(owner, alarm_payload):
            skipped_network += 1
            logger.info(
                "Smart-home webhook skip owner=%s zone=%s type=%s related_networks=%s",
                owner.id,
                getattr(owner, "zone_id", ""),
                msg_type,
                sorted(related_networks),
            )
            continue
        url = normalize_webhook_url(str(getattr(owner, "sn_webhook", "") or ""))
        if not url:
            skipped_no_url += 1
            continue
        if not is_valid_webhook_url(url):
            logger.warning(
                "Ignoring invalid smart-home webhook for owner %s: %s",
                owner.id,
                url[:80],
            )
            skipped_no_url += 1
            continue
        targets.append((owner, url))

    if not targets:
        logger.info(
            "Smart-home webhook type=%s no targets (owners=%s skipped_network=%d "
            "skipped_no_url=%d related_networks=%s origin=%s)",
            msg_type,
            unique_ids,
            skipped_network,
            skipped_no_url,
            sorted(related_networks),
            origin_network,
        )
        return {
            "webhook_sent": 0,
            "webhook_failed": 0,
            "webhook_no_urls": True,
            "webhook_skipped_network_count": skipped_network,
            "webhook_skipped_no_url_count": skipped_no_url,
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
        "Smart-home webhook type=%s origin=%s related=%s targets=%d sent=%d "
        "failed=%d skipped_network=%d skipped_no_url=%d",
        msg_type,
        origin_network,
        sorted(related_networks),
        len(targets),
        sent,
        failed,
        skipped_network,
        skipped_no_url,
    )
    return {
        "webhook_sent": sent,
        "webhook_failed": failed,
        "webhook_targets": len(targets),
        "webhook_skipped_network_count": skipped_network,
        "webhook_skipped_no_url_count": skipped_no_url,
    }
