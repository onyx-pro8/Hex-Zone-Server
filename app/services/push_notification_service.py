"""Mobile push delivery for geo-propagated messages (Expo / FCM / APNS)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import session_maker
from app.domain.message_types import (
    MessagePriority,
    is_alarm_push_type,
    is_pushable_geo_type,
    normalize_message_type,
    type_priority,
)
from app.models import PushToken

logger = logging.getLogger(__name__)

FCM_LEGACY_URL = "https://fcm.googleapis.com/fcm/send"
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"

# Must match `defaultChannel` in Hex-Zone-Mobile/app.json (expo-notifications).
ANDROID_DEFAULT_PUSH_CHANNEL = "default"
ANDROID_ALARM_PUSH_CHANNEL = "alarms"
# NS_PANIC routes to a dedicated channel so it is audibly/visually distinct
# from PANIC (see ensureAndroidChannels in Hex-Zone-Mobile/src/lib/notifications.ts).
ANDROID_NS_PANIC_PUSH_CHANNEL = "ns_panic"


def _alert_style(message_type: str) -> str:
    """Coarse client-side rendering hint carried in the push data payload."""
    upper = str(message_type or "").strip().upper().replace("-", "_")
    if upper == "NS_PANIC":
        return "ns_panic"
    if upper == "PANIC":
        return "panic"
    if is_alarm_push_type(message_type):
        return "alarm"
    return "alert"


def _android_channel_for(message_type: str) -> str:
    upper = str(message_type or "").strip().upper().replace("-", "_")
    if upper == "NS_PANIC":
        return ANDROID_NS_PANIC_PUSH_CHANNEL
    if is_alarm_push_type(message_type):
        return ANDROID_ALARM_PUSH_CHANNEL
    return ANDROID_DEFAULT_PUSH_CHANNEL


def _notification_copy(message_type: str, text: str) -> tuple[str, str]:
    upper = str(message_type or "").strip().upper().replace("-", "_")
    body = (text or "").strip()[:240]
    if upper == "PANIC":
        return "🚨 PANIC — immediate help needed", body or "A PANIC alarm was raised in your area."
    if upper == "NS_PANIC":
        return "🔕 NS-PANIC — silent distress", body or "A non-silent panic alarm was raised in your area."
    label = upper.replace("_", " ") or "ALARM"
    return f"Hex Zone {label}", body or label


async def _dispatch_to_tokens(
    tokens: list[PushToken],
    *,
    title: str,
    body: str,
    data: dict[str, str],
    channel_id: str,
    fetch_receipts: bool = False,
) -> dict[str, Any]:
    """Shared transport: routes each token to Expo/FCM/APNS by `platform`.

    Returns counts so callers can include `push_sent` / `push_failed` in their
    response payload. Never raises — transport failures are logged so a missing
    push credential never breaks the parent request (e.g. message create).
    """
    sent = 0
    failed = 0

    expo_tokens = [t.token for t in tokens if str(t.platform).upper() == "EXPO"]
    fcm_tokens = [t.token for t in tokens if str(t.platform).upper() == "FCM"]
    apns_tokens = [t.token for t in tokens if str(t.platform).upper() == "APNS"]

    delivery_errors: list[dict[str, Any]] = []
    if expo_tokens:
        expo_sent, expo_failed, expo_errors = await _send_expo_batch(
            expo_tokens,
            title=title,
            body=body,
            data=data,
            channel_id=channel_id,
            fetch_receipts=fetch_receipts,
        )
        sent += expo_sent
        failed += expo_failed
        delivery_errors.extend(expo_errors)

    fcm_key = getattr(settings, "FCM_SERVER_KEY", "") or ""
    if fcm_tokens and fcm_key:
        fcm_sent, fcm_failed = await _send_fcm_legacy_batch(
            fcm_key,
            fcm_tokens,
            title=title,
            body=body,
            data=data,
        )
        sent += fcm_sent
        failed += fcm_failed
    elif fcm_tokens and not fcm_key:
        logger.warning(
            "FCM_SERVER_KEY not configured; skipped %d FCM token(s)", len(fcm_tokens)
        )

    if apns_tokens:
        apns_sent, apns_failed = await _send_apns_batch(
            apns_tokens,
            title=title,
            body=body,
            data=data,
        )
        sent += apns_sent
        failed += apns_failed

    result: dict[str, Any] = {"push_sent": sent, "push_failed": failed}
    if delivery_errors:
        result["delivery_errors"] = delivery_errors
    return result


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
        logger.info(
            "Push skipped (%s): no active push_tokens for owners=%s",
            msg_type,
            owner_ids,
        )
        return {"push_sent": 0, "push_failed": 0, "push_no_tokens": True}

    title, body = _notification_copy(msg_type, str(alarm_payload.get("text") or ""))
    data_payload: dict[str, str] = {
        "event": "NEW_GEO_MESSAGE",
        "type": msg_type,
        "category": str(alarm_payload.get("category") or ""),
        "scope": str(alarm_payload.get("scope") or ""),
        "id": str(alarm_payload.get("id") or ""),
        "alert_style": _alert_style(msg_type),
        "priority": str(alarm_payload.get("priority") or ""),
    }
    metadata = alarm_payload.get("metadata")
    if isinstance(metadata, dict):
        position = metadata.get("position")
        if isinstance(position, dict):
            data_payload["latitude"] = str(position.get("latitude", ""))
            data_payload["longitude"] = str(position.get("longitude", ""))

    logger.info(
        "Push attempt %s -> owners=%s tokens=%d (%s)",
        msg_type,
        owner_ids,
        len(tokens),
        ", ".join(sorted({str(t.platform).upper() for t in tokens})),
    )
    return await _dispatch_to_tokens(
        tokens,
        title=title,
        body=body,
        data=data_payload,
        channel_id=_android_channel_for(msg_type),
    )


async def _retry_delivery_loop(owner_ids: list[int], payload: dict[str, Any]) -> None:
    """Re-push MAX-priority alarms until every token delivers (bounded retries).

    Runs as a detached asyncio task so the originating request returns after the
    first attempt. Each retry opens its own short-lived DB session so it never
    touches the request-scoped session that has already been closed.
    """
    attempts = max(0, int(getattr(settings, "PANIC_PUSH_RETRY_MAX_ATTEMPTS", 4)))
    delay = max(1, int(getattr(settings, "PANIC_PUSH_RETRY_DELAY_SECONDS", 15)))
    msg_type = str(payload.get("type") or "")
    for attempt in range(1, attempts + 1):
        await asyncio.sleep(delay)
        db = session_maker()
        try:
            stats = await send_alarm_push_to_owners(db, owner_ids, payload)
        except Exception:  # pragma: no cover - never crash the background task
            logger.exception("PANIC push retry attempt %d failed (%s)", attempt, msg_type)
            db.close()
            continue
        finally:
            db.close()
        sent = int(stats.get("push_sent") or 0)
        failed = int(stats.get("push_failed") or 0)
        no_tokens = bool(stats.get("push_no_tokens"))
        logger.info(
            "PANIC push retry %d/%d type=%s sent=%d failed=%d no_tokens=%s",
            attempt, attempts, msg_type, sent, failed, no_tokens,
        )
        if failed == 0 and not no_tokens and sent > 0:
            logger.info("PANIC push retry: delivered to all recipients after attempt %d", attempt)
            return
    logger.warning(
        "PANIC push retry exhausted after %d attempt(s) for %s; some recipients may be undelivered",
        attempts, msg_type,
    )


def schedule_panic_retries_if_needed(
    owner_ids: list[int],
    payload: dict[str, Any],
    stats: dict[str, Any],
) -> bool:
    """Schedule background re-delivery for MAX-priority alarms that did not fully deliver."""
    if not owner_ids:
        return False
    try:
        canonical = normalize_message_type(str(payload.get("type") or ""))
    except ValueError:
        return False
    if type_priority(canonical) != MessagePriority.MAX:
        return False
    failed = int(stats.get("push_failed") or 0)
    no_tokens = bool(stats.get("push_no_tokens"))
    if failed <= 0 and not no_tokens:
        return False
    if int(getattr(settings, "PANIC_PUSH_RETRY_MAX_ATTEMPTS", 4)) <= 0:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    loop.create_task(_retry_delivery_loop(list(owner_ids), dict(payload)))
    logger.info(
        "PANIC push retry scheduled for %s (owners=%d, failed=%d, no_tokens=%s)",
        canonical.value, len(owner_ids), failed, no_tokens,
    )
    return True


async def send_test_push_to_owner(
    db: Session,
    owner_id: int,
    *,
    title: str = "Hex Zone test push",
    body: str = "If you can read this, push delivery works end to end.",
) -> dict[str, Any]:
    """Send a self-test push to every active token of `owner_id`.

    Used by the diagnostic `POST /devices/push-token/test` endpoint. Bypasses
    the geo-propagation filter so the caller can verify Expo + FCM credentials
    against their own device, even with a single-account setup. Best-effort —
    on transport failure the helper returns counts and logs the cause.
    """
    tokens = (
        db.query(PushToken)
        .filter(PushToken.owner_id == owner_id, PushToken.active.is_(True))
        .all()
    )
    if not tokens:
        logger.warning("Test push: no active push_tokens for owner_id=%s", owner_id)
        return {
            "push_sent": 0,
            "push_failed": 0,
            "tokens": 0,
            "push_no_tokens": True,
        }

    logger.info(
        "Test push attempt -> owner=%s tokens=%d (%s)",
        owner_id,
        len(tokens),
        ", ".join(sorted({str(t.platform).upper() for t in tokens})),
    )

    data_payload: dict[str, str] = {
        "event": "TEST_PUSH",
        "type": "TEST",
        "owner_id": str(owner_id),
    }
    counts = await _dispatch_to_tokens(
        tokens,
        title=title,
        body=body,
        data=data_payload,
        channel_id=ANDROID_DEFAULT_PUSH_CHANNEL,
        fetch_receipts=True,
    )
    return {**counts, "tokens": len(tokens)}


async def _fetch_expo_receipts(
    ticket_ids: list[str],
    *,
    client: httpx.AsyncClient,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    """Poll Expo push receipts (~2s after send) for FCM/APNS delivery outcome."""
    if not ticket_ids:
        return []
    await asyncio.sleep(2)
    errors: list[dict[str, Any]] = []
    try:
        response = await client.post(
            EXPO_RECEIPTS_URL,
            headers=headers,
            json={"ids": ticket_ids},
        )
        response.raise_for_status()
        receipts = (response.json() or {}).get("data") or {}
        if not isinstance(receipts, dict):
            return errors
        for ticket_id, receipt in receipts.items():
            if not isinstance(receipt, dict):
                continue
            if receipt.get("status") == "ok":
                continue
            details = receipt.get("details") if isinstance(receipt.get("details"), dict) else {}
            err = {
                "ticket_id": ticket_id,
                "status": receipt.get("status"),
                "message": receipt.get("message"),
                "error": (details or {}).get("error"),
                "details": details,
            }
            errors.append(err)
            logger.warning(
                "Expo push receipt error: ticket=%s error=%s message=%s",
                ticket_id,
                err.get("error"),
                err.get("message"),
            )
    except httpx.HTTPError as exc:
        logger.warning("Expo push receipt fetch failed: %s", exc)
        errors.append({"error": "receipt_fetch_failed", "message": str(exc)})
    return errors


async def _send_expo_batch(
    tokens: list[str],
    *,
    title: str,
    body: str,
    data: dict[str, str],
    channel_id: str,
    fetch_receipts: bool = False,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Expo Push HTTP/2 API. Accepts both ``ExponentPushToken[...]`` and ``ExpoPushToken[...]``.

    Surfaces per-ticket errors at WARNING level with the full diagnostic shape
    Expo returns (status, message, details.error). The most common cause of a
    background-only failure is `details.error == "InvalidCredentials"` /
    `MismatchSenderId` — the FCM v1 service account JSON has not been uploaded
    on EAS for this project (`eas credentials` → Android → Push Notifications).
    """
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
    delivery_errors: list[dict[str, Any]] = []
    ticket_ids_for_receipts: list[str] = []
    chunk_size = 100
    async with httpx.AsyncClient(timeout=20.0) as client:
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
                    for token, ticket in zip(chunk, tickets):
                        if isinstance(ticket, dict) and ticket.get("status") == "ok":
                            sent += 1
                            ticket_id = ticket.get("id")
                            if fetch_receipts and isinstance(ticket_id, str) and ticket_id:
                                ticket_ids_for_receipts.append(ticket_id)
                            continue
                        failed += 1
                        if isinstance(ticket, dict):
                            details = ticket.get("details") if isinstance(ticket.get("details"), dict) else {}
                            delivery_errors.append(
                                {
                                    "phase": "ticket",
                                    "token_prefix": str(token)[:24],
                                    "status": ticket.get("status"),
                                    "message": ticket.get("message"),
                                    "error": (details or {}).get("error"),
                                    "details": details,
                                }
                            )
                            logger.warning(
                                "Expo push ticket error: status=%s error=%s message=%s details=%s token=%s…",
                                ticket.get("status"),
                                (details or {}).get("error"),
                                ticket.get("message"),
                                details,
                                str(token)[:24],
                            )
                else:
                    failed += len(chunk)
                    logger.warning("Expo push: unexpected response shape: %s", body_json)
            except httpx.HTTPStatusError as exc:
                response_text = ""
                try:
                    response_text = exc.response.text[:500]
                except Exception:  # noqa: BLE001
                    response_text = "<unreadable>"
                logger.warning(
                    "Expo push batch HTTP %s (%d tokens): %s",
                    exc.response.status_code,
                    len(chunk),
                    response_text,
                )
                failed += len(chunk)
            except httpx.HTTPError as exc:
                logger.warning("Expo push batch failed (%d tokens): %s", len(chunk), exc)
                failed += len(chunk)

        if fetch_receipts and ticket_ids_for_receipts:
            receipt_errors = await _fetch_expo_receipts(
                ticket_ids_for_receipts,
                client=client,
                headers=headers,
            )
            if receipt_errors:
                delivery_errors.extend(receipt_errors)
                failed += len(receipt_errors)
                sent = max(0, sent - len(receipt_errors))

    return sent, failed, delivery_errors


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
                if failure:
                    logger.warning(
                        "FCM legacy batch: %d/%d failed; results=%s",
                        failure,
                        len(chunk),
                        body_json.get("results"),
                    )
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
