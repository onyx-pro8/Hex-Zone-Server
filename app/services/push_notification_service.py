"""Mobile push delivery for geo-propagated messages (Expo / FCM / APNS)."""
from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.message_types import is_alarm_push_type, is_pushable_geo_type
from app.models import PushToken

logger = logging.getLogger(__name__)

FCM_LEGACY_URL = "https://fcm.googleapis.com/fcm/send"
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _notification_copy(message_type: str, text: str) -> tuple[str, str]:
    label = str(message_type or "ALARM").replace("_", " ")
    body = (text or label).strip()[:240]
    return f"Hex Zone {label}", body or label


async def send_alarm_push_to_owners(
    db: Session,
    owner_ids: list[int],
    alarm_payload: dict[str, Any],
) -> dict[str, Any]:
    """Best-effort push for geo-propagated messages.

    Includes alarm types (PANIC, SENSOR, …) and alert types (PRIVATE, PA,
    SERVICE, WELLNESS_CHECK). Never raises; transport failures are logged.
    """
    msg_type = str(alarm_payload.get("type") or "")
    if not is_pushable_geo_type(msg_type):
        return {"push_sent": 0, "push_failed": 0, "push_skipped": True}

    if not owner_ids:
        return {"push_sent": 0, "push_failed": 0}

    tokens = (
        db.query(PushToken)
        .filter(
            PushToken.owner_id.in_(owner_ids),
            PushToken.active.is_(True),
        )
        .all()
    )
    if not tokens:
        return {"push_sent": 0, "push_failed": 0, "push_no_tokens": True}

    title, body = _notification_copy(msg_type, str(alarm_payload.get("text") or ""))
    is_alarm = is_alarm_push_type(msg_type)
    data_payload = {
        "event": "NEW_GEO_MESSAGE",
        "type": msg_type,
        "category": str(alarm_payload.get("category") or ""),
        "scope": str(alarm_payload.get("scope") or ""),
        "id": str(alarm_payload.get("id") or ""),
    }
    metadata = alarm_payload.get("metadata")
    if isinstance(metadata, dict):
        position = metadata.get("position")
        if isinstance(position, dict):
            data_payload["latitude"] = str(position.get("latitude", ""))
            data_payload["longitude"] = str(position.get("longitude", ""))

    fcm_key = getattr(settings, "FCM_SERVER_KEY", "") or ""
    sent = 0
    failed = 0

    expo_tokens = [t.token for t in tokens if str(t.platform).upper() == "EXPO"]
    fcm_tokens = [t.token for t in tokens if str(t.platform).upper() == "FCM"]
    apns_tokens = [t.token for t in tokens if str(t.platform).upper() == "APNS"]

    if expo_tokens:
        expo_sent, expo_failed = await _send_expo_batch(
            expo_tokens,
            title=title,
            body=body,
            data=data_payload,
            channel_id="alarms" if is_alarm else "messages",
        )
        sent += expo_sent
        failed += expo_failed

    if fcm_tokens and fcm_key:
        fcm_sent, fcm_failed = await _send_fcm_legacy_batch(
            fcm_key,
            fcm_tokens,
            title=title,
            body=body,
            data=data_payload,
        )
        sent += fcm_sent
        failed += fcm_failed
    elif fcm_tokens and not fcm_key:
        logger.info("FCM_SERVER_KEY not configured; skipped %d FCM token(s)", len(fcm_tokens))

    if apns_tokens:
        apns_sent, apns_failed = await _send_apns_batch(
            apns_tokens,
            title=title,
            body=body,
            data=data_payload,
        )
        sent += apns_sent
        failed += apns_failed

    return {"push_sent": sent, "push_failed": failed}


async def _send_expo_batch(
    tokens: list[str],
    *,
    title: str,
    body: str,
    data: dict[str, str],
    channel_id: str,
) -> tuple[int, int]:
    """Expo Push HTTP/2 API. Accepts both ``ExponentPushToken[...]`` and ``ExpoPushToken[...]``."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
    }
    expo_access_token = getattr(settings, "EXPO_ACCESS_TOKEN", "") or ""
    if expo_access_token:
        headers["Authorization"] = f"Bearer {expo_access_token}"

    sent = 0
    failed = 0
    chunk_size = 100
    async with httpx.AsyncClient(timeout=15.0) as client:
        for offset in range(0, len(tokens), chunk_size):
            chunk = tokens[offset : offset + chunk_size]
            payload = [
                {
                    "to": token,
                    "title": title,
                    "body": body,
                    "data": data,
                    "sound": "default",
                    "priority": "high",
                    "channelId": channel_id,
                }
                for token in chunk
            ]
            try:
                response = await client.post(EXPO_PUSH_URL, headers=headers, json=payload)
                response.raise_for_status()
                body_json = response.json() or {}
                tickets = body_json.get("data") or []
                if isinstance(tickets, list):
                    for ticket in tickets:
                        if isinstance(ticket, dict) and ticket.get("status") == "ok":
                            sent += 1
                        else:
                            failed += 1
                            if isinstance(ticket, dict):
                                logger.info(
                                    "Expo push ticket error: %s",
                                    ticket.get("message") or ticket.get("details") or ticket,
                                )
                else:
                    failed += len(chunk)
                    logger.warning("Expo push: unexpected response shape: %s", body_json)
            except httpx.HTTPError as exc:
                logger.warning("Expo push batch failed (%d tokens): %s", len(chunk), exc)
                failed += len(chunk)
    return sent, failed


async def _send_fcm_legacy_batch(
    server_key: str,
    tokens: list[str],
    *,
    title: str,
    body: str,
    data: dict[str, str],
) -> tuple[int, int]:
    """Firebase legacy HTTP API (registration_ids batch, max 1000 per call)."""
    headers = {
        "Authorization": f"key={server_key}",
        "Content-Type": "application/json",
    }
    sent = 0
    failed = 0
    chunk_size = 500
    async with httpx.AsyncClient(timeout=15.0) as client:
        for offset in range(0, len(tokens), chunk_size):
            chunk = tokens[offset : offset + chunk_size]
            payload = {
                "registration_ids": chunk,
                "notification": {"title": title, "body": body},
                "data": data,
                "priority": "high",
            }
            try:
                response = await client.post(FCM_LEGACY_URL, headers=headers, json=payload)
                response.raise_for_status()
                body_json = response.json()
                success = int(body_json.get("success", 0))
                failure = int(body_json.get("failure", 0))
                sent += success
                failed += failure
            except httpx.HTTPError as exc:
                logger.warning("FCM batch failed (%d tokens): %s", len(chunk), exc)
                failed += len(chunk)
    return sent, failed


async def _send_apns_batch(
    tokens: list[str],
    *,
    title: str,
    body: str,
    data: dict[str, str],
) -> tuple[int, int]:
    """APNS HTTP/2 requires certificates; log and skip until configured."""
    apns_url = getattr(settings, "APNS_HTTP_URL", "") or ""
    apns_key = getattr(settings, "APNS_AUTH_KEY", "") or ""
    if not apns_url or not apns_key:
        logger.info(
            "APNS not configured (APNS_HTTP_URL / APNS_AUTH_KEY); skipped %d APNS token(s)",
            len(tokens),
        )
        return 0, 0

    # Minimal hook for future APNS provider wiring; keep best-effort semantics.
    _ = (title, body, data)
    logger.warning("APNS_AUTH_KEY set but APNS sender not implemented; %d token(s) skipped", len(tokens))
    return 0, len(tokens)
