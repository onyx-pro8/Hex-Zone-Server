"""Outbound smart-home webhook delivery for geo-propagated alarms/alerts.

When a smart-home device has its own webhook and linked zones, Hex Zone POSTs
each delivered geo message that matches that hub:

- Zone-based types (Panic, Sensor, …): only hubs linked to the zones used in
  the message event.
- Unknown: every hub linked to a zone on the network used in the message event.
- Guest / permission notices without zone record ids: network fallback.

Owners must already be in the delivery set. Failures never fail the request.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.message_types import CanonicalMessageType, is_pushable_geo_type
from app.models import Device, Owner, Zone
from app.services.device_entitlements import is_smart_home_hid

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


def owner_registered_smart_home_hid(db: Session, owner: Owner) -> str | None:
    """Return a registered DEV- hub HID for this owner, or None if none exist.

    Prefers ``owners.sn_hid`` when it still matches an active hub.
    """
    devices = (
        db.query(Device)
        .filter(Device.owner_id == owner.id, Device.active.is_(True))
        .order_by(Device.created_at.asc())
        .all()
    )
    hubs = [device for device in devices if is_smart_home_hid(device.hid)]
    if not hubs:
        return None
    preferred = str(getattr(owner, "sn_hid", "") or "").strip()
    if preferred:
        preferred_key = preferred.upper()
        for hub in hubs:
            hid = str(hub.hid or "").strip()
            if hid.upper() == preferred_key:
                return hid
    return str(hubs[0].hid or "").strip() or None


def _add_zone_record_id(bucket: set[int], value: Any) -> None:
    try:
        bucket.add(int(value))
    except (TypeError, ValueError):
        return


def device_zone_record_ids(device: Device) -> set[int]:
    """Zone record ids stored on a smart-home device."""
    raw = getattr(device, "zone_ids", None)
    if not isinstance(raw, list):
        return set()
    found: set[int] = set()
    for item in raw:
        _add_zone_record_id(found, item)
    return found


def message_zone_record_ids_for_owner(
    alarm_payload: dict[str, Any],
    owner_id: int,
) -> set[int]:
    """Zone records this delivery is about for one recipient."""
    metadata = _as_dict(alarm_payload.get("metadata"))
    found: set[int] = set()
    mapping = metadata.get("recipient_zone_record_ids")
    if isinstance(mapping, dict):
        raw = mapping.get(str(owner_id))
        if raw is None:
            raw = mapping.get(owner_id)
        if raw is not None:
            _add_zone_record_id(found, raw)

    sender_raw = alarm_payload.get("sender_id")
    try:
        sender_id = int(sender_raw) if sender_raw is not None else None
    except (TypeError, ValueError):
        sender_id = None
    if sender_id is not None and sender_id == int(owner_id):
        raw = metadata.get("sender_relevant_zone_record_id")
        if raw is not None:
            _add_zone_record_id(found, raw)

    fanout = _as_dict(metadata.get("fanout"))
    for key in (
        "primary_zone_record_ids",
        "sender_zone_record_ids",
        "secondary_zone_record_ids",
        "matched_zone_record_ids",
    ):
        values = fanout.get(key)
        if isinstance(values, list):
            for item in values:
                _add_zone_record_id(found, item)
    return found


def is_unknown_hub_message(alarm_payload: dict[str, Any]) -> bool:
    """Unknown has no message zones; hubs match by network instead."""
    return str(alarm_payload.get("type") or "").strip().upper() == (
        CanonicalMessageType.UNKNOWN.value
    )


def device_matches_message_network(
    db: Session,
    linked_zone_ids: set[int],
    alarm_payload: dict[str, Any],
) -> bool:
    """True when a linked zone belongs to a network on the message event."""
    if not linked_zone_ids:
        return False
    networks = {net.casefold() for net in message_related_network_ids(alarm_payload)}
    if not networks:
        return False
    for zone_id in linked_zone_ids:
        row = db.get(Zone, zone_id)
        if row is None:
            continue
        label = str(getattr(row, "zone_id", "") or "").strip()
        if label and label.casefold() in networks:
            return True
    return False


def device_matches_message_zones(
    db: Session,
    device: Device,
    owner_id: int,
    alarm_payload: dict[str, Any],
) -> bool:
    """True when this hub should receive the message webhook.

    - Unknown: any linked zone on the message network.
    - Zone-based with zone record ids: intersection with hub ``zone_ids``.
    - Notices without zone records (guest / permission): network fallback.
    """
    linked = device_zone_record_ids(device)
    if not linked:
        return False

    if is_unknown_hub_message(alarm_payload):
        return device_matches_message_network(db, linked, alarm_payload)

    related = message_zone_record_ids_for_owner(alarm_payload, owner_id)
    if related:
        return bool(linked & related)

    return device_matches_message_network(db, linked, alarm_payload)


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


def _resolve_broadcast_name(
    db: Session | None,
    alarm_payload: dict[str, Any],
) -> str:
    """Sender display name for the webhook title."""
    sender_id = alarm_payload.get("sender_id")
    if sender_id is None:
        return "Guest"
    try:
        sender_id_int = int(sender_id)
    except (TypeError, ValueError):
        return "Unknown"
    if db is None:
        return "Unknown"
    sender = db.query(Owner).filter(Owner.id == sender_id_int).first()
    if sender is None:
        return "Unknown"
    return str(getattr(sender, "message_display_name", None) or "Unknown").strip() or "Unknown"


def _clean_zone_heading(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _compose_zone_heading(name: object, network_id: object) -> str | None:
    clean_name = _clean_zone_heading(name)
    clean_net = _clean_zone_heading(network_id)
    if clean_name and clean_net:
        return f"{clean_name} ({clean_net})"
    return clean_name or clean_net


def _resolve_zone_name(
    metadata: dict[str, Any],
    *,
    recipient_owner_id: int,
    sender_id: int | None = None,
    db: Session | None = None,
) -> str:
    """Same zone heading the inbox bubble uses for this delivery.

    1. Embedded recipient/sender zone label (or name + network id)
    2. ``resolve_relevant_zone_for_viewer`` (DB / fanout record-id fallback)
    """
    recipient_zones = _as_dict(metadata.get("recipient_relevant_zones"))
    recip = _as_dict(recipient_zones.get(str(recipient_owner_id)))
    sender_zone = _as_dict(metadata.get("sender_relevant_zone"))
    sender_net = _clean_zone_heading(metadata.get("sender_network_id"))

    for bag in (recip, sender_zone):
        if not bag:
            continue
        label = _clean_zone_heading(bag.get("label"))
        if label:
            return label
        name = _clean_zone_heading(bag.get("name"))
        if name:
            heading = _compose_zone_heading(
                name,
                sender_net or bag.get("sender_network_id") or bag.get("network_id"),
            )
            if heading:
                return heading

    if db is not None:
        from app.services.message_relevant_zone_service import (
            resolve_relevant_zone_for_viewer,
        )

        fields = resolve_relevant_zone_for_viewer(
            db,
            metadata=metadata,
            viewer_owner_id=int(recipient_owner_id),
            sender_id=sender_id,
        )
        for key in (
            "relevant_zone_label",
            "relevant_zone_name",
            "relevant_zone_network_id",
        ):
            heading = _clean_zone_heading(fields.get(key))
            if heading:
                return heading

    return "Unknown zone"


def _format_location(metadata: dict[str, Any]) -> str:
    position = _as_dict(metadata.get("position"))
    lat = position.get("latitude")
    lon = position.get("longitude")
    try:
        lat_f = float(lat) if lat is not None else None
        lon_f = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        return "unknown"
    if lat_f is None or lon_f is None:
        return "unknown"
    return f"{lat_f}, {lon_f}"


def build_smart_home_webhook_payload(
    alarm_payload: dict[str, Any],
    *,
    recipient_owner_id: int,
    network_id: str | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """JSON body for client Home Assistant webhooks.

    Contract (client automation)::

        title   -> trigger.json.title
                   ``{broadcast_name} · {zone_name}``
        message -> trigger.json.message
                   ``{TYPE}: {text}`` plus ``Location: {lat}, {lon}``

    Extra Hex Zone diagnostics are omitted so hubs only see the HA fields.
    ``network_id`` is accepted for call-site compatibility but not included
    in the POST body.
    """
    del network_id  # reserved for future hub routing
    metadata = _as_dict(alarm_payload.get("metadata"))
    msg_type = str(alarm_payload.get("type") or "").strip().upper()
    text = str(alarm_payload.get("text") or "").strip()
    if not text:
        msg = _as_dict(metadata.get("msg"))
        text = str(
            msg.get("description")
            or msg.get("title")
            or msg.get("text")
            or ""
        ).strip()

    broadcast_name = _resolve_broadcast_name(db, alarm_payload)
    sender_id_raw = alarm_payload.get("sender_id")
    try:
        sender_id = int(sender_id_raw) if sender_id_raw is not None else None
    except (TypeError, ValueError):
        sender_id = None
    zone_name = _resolve_zone_name(
        metadata,
        recipient_owner_id=recipient_owner_id,
        sender_id=sender_id,
        db=db,
    )
    location = _format_location(metadata)

    title = f"{broadcast_name} · {zone_name}"
    if msg_type and text:
        body_line = f"{msg_type}: {text}"
    elif msg_type:
        body_line = msg_type
    elif text:
        body_line = text
    else:
        body_line = "System Update"
    message = f"{body_line}\nLocation: {location}"
    return {
        "title": title,
        "message": message,
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
    *,
    require_pushable: bool = True,
) -> dict[str, Any]:
    """POST the alarm to each eligible hub owner's webhook URL.

    Eligibility per device:
    - owner is in ``owner_ids`` and passes delivery/network rules
    - registered smart-home HID (``DEV-`` device)
    - its own valid webhook
    - zone match for zone-based types, or network match for Unknown

    When ``require_pushable`` is True (default), non-geo types such as
    ``PERMISSION`` and ``SERVICE`` are skipped. Guest-pass auto-approval and
    member-invite QR joins may pass ``require_pushable=False`` so hubs still
    get that notice while mobile alarm-push rules stay unchanged.

    Returns aggregate counts plus ``webhook_owner_results`` for per-user toasts.
    Never raises.
    """
    msg_type = str(alarm_payload.get("type") or "")
    if require_pushable and not is_pushable_geo_type(msg_type):
        logger.info("Smart-home webhook type=%s skipped: not a pushable geo type", msg_type)
        return {
            "webhook_sent": 0,
            "webhook_failed": 0,
            "webhook_skipped": True,
            "webhook_owner_results": [],
        }

    unique_ids = sorted({int(oid) for oid in owner_ids if isinstance(oid, int)})
    if not unique_ids:
        logger.info("Smart-home webhook type=%s skipped: no owner targets", msg_type)
        return {
            "webhook_sent": 0,
            "webhook_failed": 0,
            "webhook_no_targets": True,
            "webhook_owner_results": [],
        }

    related_networks = message_related_network_ids(alarm_payload)
    origin_network = message_origin_network_id(alarm_payload)

    owners = (
        db.query(Owner)
        .filter(Owner.id.in_(unique_ids), Owner.active.is_(True))
        .all()
    )
    targets: list[tuple[Owner, str, str]] = []  # owner, url, hid
    skipped_network = 0
    skipped_no_url = 0
    skipped_no_hid = 0
    skipped_no_zone = 0
    for owner in owners:
        if not owner_should_receive_webhook(owner, alarm_payload):
            skipped_network += 1
            continue
        devices = (
            db.query(Device)
            .filter(Device.owner_id == owner.id, Device.active.is_(True))
            .order_by(Device.created_at.asc())
            .all()
        )
        hubs = [device for device in devices if is_smart_home_hid(device.hid)]
        if not hubs:
            skipped_no_hid += 1
            continue
        for hub in hubs:
            url = normalize_webhook_url(str(getattr(hub, "webhook", "") or ""))
            if not url or not is_valid_webhook_url(url):
                if url:
                    logger.warning(
                        "Ignoring invalid smart-home webhook for device %s: %s",
                        getattr(hub, "hid", ""),
                        url[:80],
                    )
                skipped_no_url += 1
                continue
            if not device_matches_message_zones(db, hub, int(owner.id), alarm_payload):
                skipped_no_zone += 1
                continue
            targets.append((owner, url, str(hub.hid or "").strip()))

    if not targets:
        logger.info(
            "Smart-home webhook type=%s no targets (owners=%s skipped_network=%d "
            "skipped_no_hid=%d skipped_no_url=%d skipped_no_zone=%d "
            "related_networks=%s origin=%s)",
            msg_type,
            unique_ids,
            skipped_network,
            skipped_no_hid,
            skipped_no_url,
            skipped_no_zone,
            sorted(related_networks),
            origin_network,
        )
        return {
            "webhook_sent": 0,
            "webhook_failed": 0,
            "webhook_no_urls": skipped_no_url > 0 and skipped_no_zone == 0,
            "webhook_skipped_network_count": skipped_network,
            "webhook_skipped_no_hid_count": skipped_no_hid,
            "webhook_skipped_no_url_count": skipped_no_url,
            "webhook_skipped_no_zone_count": skipped_no_zone,
            "webhook_owner_results": [],
        }

    sent = 0
    failed = 0
    owner_results: list[dict[str, Any]] = []
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
        for owner, url, hid in targets:
            body = build_smart_home_webhook_payload(
                alarm_payload,
                recipient_owner_id=owner.id,
                network_id=str(owner.zone_id or ""),
                db=db,
            )
            ok = await _post_webhook(client, url=url, body=body)
            owner_results.append(
                {
                    "owner_id": int(owner.id),
                    "ok": ok,
                    "hid": hid,
                    "title": body.get("title") or "",
                }
            )
            if ok:
                sent += 1
            else:
                failed += 1

    logger.info(
        "Smart-home webhook type=%s origin=%s related=%s targets=%d sent=%d "
        "failed=%d skipped_network=%d skipped_no_hid=%d skipped_no_url=%d skipped_no_zone=%d",
        msg_type,
        origin_network,
        sorted(related_networks),
        len(targets),
        sent,
        failed,
        skipped_network,
        skipped_no_hid,
        skipped_no_url,
        skipped_no_zone,
    )
    return {
        "webhook_sent": sent,
        "webhook_failed": failed,
        "webhook_targets": len(targets),
        "webhook_skipped_network_count": skipped_network,
        "webhook_skipped_no_hid_count": skipped_no_hid,
        "webhook_skipped_no_url_count": skipped_no_url,
        "webhook_skipped_no_zone_count": skipped_no_zone,
        "webhook_owner_results": owner_results,
    }


async def notify_smart_home_webhook_owners(owner_results: list[dict[str, Any]]) -> None:
    """Push short success/failure toasts to each hub owner via WebSocket."""
    if not owner_results:
        return
    from app.websocket.manager import ws_manager

    for row in owner_results:
        try:
            owner_id = int(row.get("owner_id"))
        except (TypeError, ValueError):
            continue
        ok = bool(row.get("ok"))
        payload = {
            "ok": ok,
            "hid": str(row.get("hid") or ""),
            "title": str(row.get("title") or ""),
            "toast": "Hub notified." if ok else "Hub webhook failed.",
        }
        try:
            await ws_manager.broadcast_to_users(
                [owner_id],
                "SMART_HOME_WEBHOOK",
                payload,
            )
        except Exception:  # pragma: no cover - never break alarm delivery
            logger.exception(
                "Smart-home webhook WS notify failed for owner %s", owner_id
            )


def build_universal_access_open_payload(
    *,
    zone_id: str,
    email: str,
    name: str,
) -> dict[str, str]:
    """Body posted when someone joins a zone via universal access."""
    return {
        "zone_id": str(zone_id or "").strip(),
        "email": str(email or "").strip(),
        "name": str(name or "").strip(),
        "action": "open",
    }


async def send_universal_access_open_webhooks(
    db: Session,
    *,
    hub_owner_id: int,
    zone_record_ids: list[int],
    member_email: str,
    member_name: str,
) -> dict[str, Any]:
    """POST ``{zone_id, email, name, action: open}`` to hubs linked to those zones.

    Only hubs owned by ``hub_owner_id`` (the network admin) that list a granted
    zone in ``device.zone_ids`` are notified. Never raises.
    """
    unique_zone_ids: list[int] = []
    seen: set[int] = set()
    for raw in zone_record_ids:
        try:
            zone_id = int(raw)
        except (TypeError, ValueError):
            continue
        if zone_id < 1 or zone_id in seen:
            continue
        seen.add(zone_id)
        unique_zone_ids.append(zone_id)

    if not unique_zone_ids:
        return {
            "ua_open_sent": 0,
            "ua_open_failed": 0,
            "ua_open_skipped": True,
        }

    try:
        owner_id = int(hub_owner_id)
    except (TypeError, ValueError):
        return {
            "ua_open_sent": 0,
            "ua_open_failed": 0,
            "ua_open_skipped": True,
        }

    devices = (
        db.query(Device)
        .filter(Device.owner_id == owner_id, Device.active.is_(True))
        .order_by(Device.created_at.asc())
        .all()
    )
    hubs = [device for device in devices if is_smart_home_hid(device.hid)]
    if not hubs:
        return {
            "ua_open_sent": 0,
            "ua_open_failed": 0,
            "ua_open_no_hubs": True,
        }

    targets: list[tuple[str, dict[str, str]]] = []
    for zone_record_id in unique_zone_ids:
        zone = db.get(Zone, zone_record_id)
        if zone is None:
            continue
        zone_label = str(getattr(zone, "zone_id", "") or "").strip() or str(zone.id)
        body = build_universal_access_open_payload(
            zone_id=zone_label,
            email=member_email,
            name=member_name,
        )
        for hub in hubs:
            if zone_record_id not in device_zone_record_ids(hub):
                continue
            url = normalize_webhook_url(str(getattr(hub, "webhook", "") or ""))
            if not url or not is_valid_webhook_url(url):
                continue
            targets.append((url, body))

    if not targets:
        return {
            "ua_open_sent": 0,
            "ua_open_failed": 0,
            "ua_open_no_targets": True,
        }

    sent = 0
    failed = 0
    timeout = _webhook_timeout_seconds()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for url, body in targets:
                ok = await _post_webhook(client, url=url, body=body)
                if ok:
                    sent += 1
                else:
                    failed += 1
    except Exception:  # pragma: no cover - never break UA join
        logger.exception(
            "Universal-access open webhook batch failed for owner %s", owner_id
        )
        failed += max(0, len(targets) - sent)

    logger.info(
        "Universal-access open webhooks owner=%s zones=%s targets=%d sent=%d failed=%d",
        owner_id,
        unique_zone_ids,
        len(targets),
        sent,
        failed,
    )
    return {
        "ua_open_sent": sent,
        "ua_open_failed": failed,
        "ua_open_targets": len(targets),
    }
