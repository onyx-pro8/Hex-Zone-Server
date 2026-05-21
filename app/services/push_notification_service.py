"""Mobile push delivery for alarm-category geo messages (FCM / APNS)."""
from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.message_types import is_alarm_push_type
from app.models import PushToken

logger = logging.getLogger(__name__)

FCM_LEGACY_URL = "https://fcm.googleapis.com/fcm/send"


def _alarm_notification_copy(message_type: str, text: str) -> tuple[str, str]:
    label = str(message_type or "ALARM").replace("_", " ")
    body = (text or label).strip()[:240]
    return f"Hex Zone {label}", body or label


async def send_alarm_push_to_owners(
    db: Session,
    owner_ids: list[int],
    alarm_payload: dict[str, Any],
) -> dict[str, Any]:
    """Best-effort push for alarm types. Never raises; failures are logged only."""
    msg_type = str(alarm_payload.get("type") or "")
    if not is_alarm_push_type(msg_type):
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

    title, body = _alarm_notification_copy(msg_type, str(alarm_payload.get("text") or ""))
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

    fcm_tokens = [t.token for t in tokens if str(t.platform).upper() == "FCM"]
    apns_tokens = [t.token for t in tokens if str(t.platform).upper() == "APNS"]

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
