"""Long-running startup jobs that must not block FastAPI request handlers.

Currently this hosts the owner-address geocoding backfill: every owner with a
stored address is re-geocoded into ``owners.latitude / longitude`` so stale GPS
values are replaced by the registered home address.

* The application is up immediately — health checks and request handlers do
  not wait for Nominatim.
* Nominatim's 1 req/sec rate-limit is respected — we use the shared limiter in
  `area_boundary_service` and add an extra sleep between owners as defence in
  depth.
* Failures are silent and the job moves on; the next deploy / restart will
  retry any owner whose address still didn't resolve.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.database import session_maker
from app.domain.message_types import CanonicalMessageType
from app.models import Owner
from app.models.wellness_check_acknowledgement import WellnessCheckAcknowledgement
from app.models.zone_message_event import ZoneMessageEvent
from app.services import push_notification_service
from app.services.owner_home_service import sync_owner_home_from_address

logger = logging.getLogger(__name__)

# Conservative pacing on top of the Nominatim 1 req/sec limiter; keeps the
# backfill polite on free-tier OpenStreetMap infrastructure even when the
# limiter window has rolled over.
_PER_OWNER_DELAY_SECONDS = 1.2
_MAX_OWNERS_PER_RUN = 200


def _select_owners_needing_geocode(db: Session) -> Iterable[Owner]:
    return (
        db.query(Owner)
        .filter(Owner.address.isnot(None))
        .filter(Owner.address != "")
        .order_by(Owner.id.asc())
        .limit(_MAX_OWNERS_PER_RUN)
        .all()
    )


def backfill_owner_coordinates() -> None:
    """Re-geocode ``owners.latitude/longitude`` from ``owners.address`` for all owners.

    Designed to be invoked from a daemon thread at app startup. The function
    catches its own exceptions so a single bad address (or a Nominatim outage)
    cannot terminate the worker thread.
    """
    db = session_maker()
    try:
        owners = _select_owners_needing_geocode(db)
        if not owners:
            logger.info("Owner geocode backfill: nothing to do")
            return
        logger.info("Owner geocode backfill: %d owner(s) queued", len(owners))
        resolved = 0
        for owner in owners:
            try:
                if sync_owner_home_from_address(owner):
                    resolved += 1
                    try:
                        db.commit()
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning("Commit failed for owner %s: %s", owner.id, exc)
                        db.rollback()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Geocode raised for owner %s: %s", owner.id, exc)
            time.sleep(_PER_OWNER_DELAY_SECONDS)
        logger.info(
            "Owner geocode backfill complete: %d resolved / %d queued",
            resolved,
            len(owners),
        )
    finally:
        db.close()


def _pending_wellness_recipient_ids(db: Session, event: ZoneMessageEvent) -> list[int]:
    """Delivered recipients (excluding sender) who have not yet acknowledged."""
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    delivered = metadata.get("delivered_owner_ids") or []
    expected = {
        int(raw)
        for raw in delivered
        if isinstance(raw, int) and raw != event.sender_id
    }
    if not expected:
        return []
    acked_rows = (
        db.query(WellnessCheckAcknowledgement.owner_id)
        .filter(WellnessCheckAcknowledgement.message_event_id == event.id)
        .all()
    )
    acked = {int(row[0]) for row in acked_rows}
    return sorted(expected - acked)


def _wellness_reminder_payload(event: ZoneMessageEvent, reminder_no: int) -> dict:
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    return {
        "id": event.id,
        "sender_id": event.sender_id,
        "type": event.type,
        "category": event.category.value if event.category else "Alert",
        "scope": event.scope.value if event.scope else "public",
        "text": (
            f"Reminder {reminder_no}: please confirm you are safe and well."
        ),
        "priority": str(metadata.get("priority") or "HIGH"),
        "metadata": metadata,
    }


def _scan_wellness_reminders_once(db: Session) -> int:
    """Send a reminder push to recipients who have not acknowledged in time.

    Returns the number of wellness events that triggered a reminder this pass.
    """
    delay = max(1, int(getattr(settings, "WELLNESS_REMINDER_DELAY_SECONDS", 300)))
    max_reminders = max(0, int(getattr(settings, "WELLNESS_REMINDER_MAX", 3)))
    lookback_hours = max(1, int(getattr(settings, "WELLNESS_REMINDER_LOOKBACK_HOURS", 24)))
    if max_reminders <= 0:
        return 0

    now = datetime.utcnow()
    lookback_since = now - timedelta(hours=lookback_hours)
    events = (
        db.query(ZoneMessageEvent)
        .filter(
            ZoneMessageEvent.type == CanonicalMessageType.WELLNESS_CHECK.value,
            ZoneMessageEvent.created_at >= lookback_since,
        )
        .order_by(ZoneMessageEvent.created_at.asc())
        .limit(200)
        .all()
    )

    reminded = 0
    for event in events:
        metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
        reminders_sent = int(metadata.get("wellness_reminders") or 0)
        if reminders_sent >= max_reminders:
            continue

        last_at_raw = metadata.get("wellness_last_reminder_at")
        anchor = event.created_at
        if isinstance(last_at_raw, str):
            try:
                anchor = datetime.fromisoformat(last_at_raw)
            except ValueError:
                anchor = event.created_at
        if (now - anchor).total_seconds() < delay:
            continue

        pending = _pending_wellness_recipient_ids(db, event)
        if not pending:
            continue

        reminder_no = reminders_sent + 1
        payload = _wellness_reminder_payload(event, reminder_no)
        try:
            stats = asyncio.run(
                push_notification_service.send_alarm_push_to_owners(db, pending, payload)
            )
        except Exception:  # pragma: no cover - never crash the worker thread
            logger.exception("Wellness reminder push failed for event %s", event.id)
            continue

        metadata["wellness_reminders"] = reminder_no
        metadata["wellness_last_reminder_at"] = now.isoformat()
        event.metadata_json = metadata
        flag_modified(event, "metadata_json")
        try:
            db.commit()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Wellness reminder commit failed for event %s", event.id)
            db.rollback()
            continue
        reminded += 1
        logger.info(
            "Wellness reminder %d/%d sent for event %s to %d recipient(s) (push=%s)",
            reminder_no, max_reminders, event.id, len(pending), stats,
        )
    return reminded


def wellness_reminder_worker() -> None:
    """Daemon loop: periodically re-push WELLNESS_CHECK to non-responders.

    Best-effort: each pass opens its own session, and all failures are logged
    so a transient DB/network issue never terminates the worker thread.
    """
    if not bool(getattr(settings, "WELLNESS_REMINDER_ENABLED", True)):
        logger.info("Wellness reminder worker disabled by config")
        return
    interval = max(
        30, int(getattr(settings, "WELLNESS_REMINDER_SCAN_INTERVAL_SECONDS", 120))
    )
    # Let init_db / schema patches settle before the first scan.
    time.sleep(45)
    logger.info("Wellness reminder worker started (interval=%ds)", interval)
    while True:
        db = session_maker()
        try:
            _scan_wellness_reminders_once(db)
        except Exception:  # pragma: no cover - never crash the worker thread
            logger.exception("Wellness reminder scan failed")
        finally:
            db.close()
        time.sleep(interval)
