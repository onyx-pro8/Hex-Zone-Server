"""Geo propagation message workflows."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.message_types import (
    CanonicalMessageType,
    MessagePriority,
    MessageScope,
    bypasses_delivery_blocks,
    enables_response_tracking,
    normalize_message_type,
    type_category,
    type_priority,
    type_scope,
)
from app.models import EmergencyEvent, Owner, ZoneMessageEvent
from app.schemas.message_feature import MessageFeatureType, PropagationMessageCreate
from app.services import message_block_service
from app.services.geospatial_service import (
    evaluate_zones_containing_point,
    owner_ids_located_within_zone_ids,
)
from app.services.unknown_fanout_service import (
    UNKNOWN_RATE_LIMIT_SECONDS,
    resolve_nearest_owner_ids_among,
    unknown_fanout_limit,
)

logger = logging.getLogger(__name__)


class UnknownRateLimitError(Exception):
    """Raised when a sender posts UNKNOWN more than once per rate-limit window."""


class SensorRateLimitError(Exception):
    """Raised when a sender posts SENSOR telemetry faster than the throttle window."""


class PrivateScopeRecipientError(ValueError):
    """Raised when a PRIVATE receiver is not a reachable same-zone/account member."""


class GeoMessageSkipped(Exception):
    """No-op path (e.g. UNKNOWN with no usable origin coordinates)."""

    def __init__(self, detail: dict):
        self.detail = detail


def _resolve_unknown_origin(
    sender: Owner,
    payload: PropagationMessageCreate,
) -> tuple[float, float, str]:
    """Origin for UNKNOWN nearest-neighbour fan-out (owner record, else message position)."""
    if sender.latitude is not None and sender.longitude is not None:
        return float(sender.latitude), float(sender.longitude), "owner_record"
    return (
        float(payload.position.latitude),
        float(payload.position.longitude),
        "message_position",
    )


def _to_canonical_type(message_type: MessageFeatureType) -> CanonicalMessageType:
    return normalize_message_type(message_type.value)


def _assert_unknown_rate_limit_ok(db: Session, sender_id: int) -> None:
    window = timedelta(seconds=getattr(settings, "UNKNOWN_MESSAGE_RATE_LIMIT_SECONDS", UNKNOWN_RATE_LIMIT_SECONDS))
    since = datetime.utcnow() - window
    recent = (
        db.query(ZoneMessageEvent.id)
        .filter(
            ZoneMessageEvent.sender_id == sender_id,
            ZoneMessageEvent.type == CanonicalMessageType.UNKNOWN.value,
            ZoneMessageEvent.created_at >= since,
        )
        .first()
    )
    if recent is not None:
        raise UnknownRateLimitError()


def _assert_sensor_rate_limit_ok(db: Session, sender_id: int) -> None:
    window = timedelta(
        seconds=getattr(settings, "SENSOR_MESSAGE_RATE_LIMIT_SECONDS", 5)
    )
    if window.total_seconds() <= 0:
        return
    since = datetime.utcnow() - window
    recent = (
        db.query(ZoneMessageEvent.id)
        .filter(
            ZoneMessageEvent.sender_id == sender_id,
            ZoneMessageEvent.type == CanonicalMessageType.SENSOR.value,
            ZoneMessageEvent.created_at >= since,
        )
        .first()
    )
    if recent is not None:
        raise SensorRateLimitError()


def _assert_private_receiver_reachable(
    db: Session,
    sender: Owner,
    receiver_owner_id: int,
    *,
    sender_zone_ids: list[str],
) -> None:
    """PRIVATE messages target one recipient who is physically inside the sender's zone(s).

    The zone here is a *permission/validation boundary*, derived from the sender's
    current GPS location (``sender_zone_ids``), not a static ``zone_id`` label.
    The selected recipient must be an active member whose own realtime location
    falls inside at least one of those same zones.
    """
    receiver = db.get(Owner, receiver_owner_id)
    if receiver is None or not bool(receiver.active):
        raise PrivateScopeRecipientError(
            "PRIVATE receiver must be an active member."
        )
    if receiver.id == sender.id:
        raise PrivateScopeRecipientError(
            "PRIVATE messages cannot target yourself."
        )

    if not sender_zone_ids:
        raise PrivateScopeRecipientError(
            "You are not currently inside any zone, so you cannot send a PRIVATE message."
        )

    located_owner_ids = set(
        owner_ids_located_within_zone_ids(db, sender_zone_ids)
    )
    if receiver.id not in located_owner_ids:
        raise PrivateScopeRecipientError(
            "PRIVATE receiver must be currently located inside the same zone as you."
        )


def _apply_delivery_blocks(
    db: Session,
    *,
    sender_id: int,
    candidate_recipients: list[int],
    message_type_value: str,
) -> tuple[list[int], list[int]]:
    delivered: list[int] = []
    blocked: list[int] = []
    for owner_id in candidate_recipients:
        if message_block_service.is_delivery_blocked(
            db,
            recipient_owner_id=owner_id,
            sender_owner_id=sender_id,
            message_type=message_type_value,
        ):
            blocked.append(owner_id)
            continue
        delivered.append(owner_id)
    return delivered, blocked


def _zone_based_recipients(
    db: Session,
    sender: Owner,
    payload: PropagationMessageCreate,
    canonical_type: CanonicalMessageType,
    scope: MessageScope,
) -> tuple[list[str], list[int], dict]:
    """Resolve recipients for geo alarm/alert types (not UNKNOWN).

    The sender's current GPS location (``payload.position``) is matched against
    zone geometry to determine which zone(s) the sender is inside. Recipients are
    then every active owner whose own **realtime location** falls inside one of
    those same zone(s) — i.e. "everyone currently in the zone".

    For PRIVATE scope the same zone(s) are used purely as a permission boundary:
    exactly one explicitly-selected recipient is delivered to, and only if that
    recipient is currently located inside one of the sender's zone(s).
    """
    latitude = float(payload.position.latitude)
    longitude = float(payload.position.longitude)
    sender_zone_ids = evaluate_zones_containing_point(db, latitude, longitude)

    if scope == MessageScope.PRIVATE and payload.receiver_owner_id is None:
        raise ValueError("receiver_owner_id is required for private-scope message types")

    if scope == MessageScope.PRIVATE:
        _assert_private_receiver_reachable(
            db, sender, payload.receiver_owner_id, sender_zone_ids=sender_zone_ids
        )
        return (
            sender_zone_ids,
            [payload.receiver_owner_id],
            {
                "strategy": "private_same_zone",
                "sender_zone_ids": sender_zone_ids,
            },
        )

    located_owner_ids = owner_ids_located_within_zone_ids(
        db,
        sender_zone_ids,
        exclude_owner_id=sender.id,
    )
    meta = {
        "strategy": "recipients_located_in_zone",
        "sender_zone_ids": sender_zone_ids,
        "located_owner_ids": located_owner_ids,
    }
    return sender_zone_ids, located_owner_ids, meta


def _log_emergency_event(
    db: Session,
    *,
    event: ZoneMessageEvent,
    canonical_type: CanonicalMessageType,
    recipient_count: int,
    position: dict | None,
) -> None:
    """Persist an immutable forensic record for a MAX-priority alarm.

    Wrapped in a SAVEPOINT and best-effort: a logging failure (e.g. the
    ``emergency_events`` table not yet created on a freshly-deployed DB) must
    never block the life-safety alarm itself, so we roll back only this nested
    insert and let the parent transaction commit the message normally.
    """
    latitude = None
    longitude = None
    if isinstance(position, dict):
        try:
            latitude = float(position.get("latitude")) if position.get("latitude") is not None else None
            longitude = float(position.get("longitude")) if position.get("longitude") is not None else None
        except (TypeError, ValueError):
            latitude = None
            longitude = None
    try:
        with db.begin_nested():
            db.add(
                EmergencyEvent(
                    message_event_id=event.id,
                    type=canonical_type.value,
                    sender_id=event.sender_id,
                    zone_id=event.zone_id,
                    recipient_count=int(recipient_count),
                    latitude=latitude,
                    longitude=longitude,
                    text=event.text,
                )
            )
            db.flush()
    except Exception:  # noqa: BLE001 - never let audit logging break an alarm
        logger.exception(
            "Emergency event logging failed for message %s (%s); alarm still delivered",
            event.id,
            canonical_type.value,
        )


def create_geo_propagated_message(db: Session, sender: Owner, payload: PropagationMessageCreate) -> dict:
    canonical_type = _to_canonical_type(payload.type)
    scope = type_scope(canonical_type)
    account_type = str(sender.account_type.value).strip().lower()

    fanout_meta: dict = {
        "account_type": account_type,
    }

    if canonical_type == CanonicalMessageType.UNKNOWN:
        _assert_unknown_rate_limit_ok(db, sender.id)
        zone_lat = float(payload.position.latitude)
        zone_lon = float(payload.position.longitude)
        origin_lat, origin_lon, origin_source = _resolve_unknown_origin(sender, payload)
        if origin_source == "message_position":
            sender.latitude = origin_lat
            sender.longitude = origin_lon
            sender.location_updated_at = datetime.utcnow()

        sender_zone_ids = evaluate_zones_containing_point(db, zone_lat, zone_lon)
        target_x = unknown_fanout_limit(sender)
        located_owner_ids = (
            owner_ids_located_within_zone_ids(
                db,
                sender_zone_ids,
                exclude_owner_id=sender.id,
            )
            if sender_zone_ids
            else []
        )
        candidate_recipients = resolve_nearest_owner_ids_among(
            db,
            origin_lat=zone_lat,
            origin_lon=zone_lon,
            candidate_owner_ids=located_owner_ids,
            limit=target_x,
        )
        zone_ids = sender_zone_ids
        fanout_meta = {
            "account_type": account_type,
            "strategy": "unknown_nearest_in_zone",
            "sender_zone_ids": sender_zone_ids,
            "located_owner_ids": located_owner_ids,
            "target_x": target_x,
            "resolved_x": len(candidate_recipients),
            "origin": {
                "latitude": origin_lat,
                "longitude": origin_lon,
                "source": origin_source,
            },
        }
        position_for_metadata = {"latitude": zone_lat, "longitude": zone_lon}
    else:
        if canonical_type == CanonicalMessageType.SENSOR:
            _assert_sensor_rate_limit_ok(db, sender.id)
        zone_ids, candidate_recipients, zone_meta = _zone_based_recipients(
            db, sender, payload, canonical_type, scope
        )
        position_for_metadata = {
            "latitude": payload.position.latitude,
            "longitude": payload.position.longitude,
        }
        fanout_meta = {
            "account_type": account_type,
            **zone_meta,
            "target_x": len(candidate_recipients),
        }

    delivered_owner_ids, blocked_owner_ids = _apply_delivery_blocks(
        db,
        sender_id=sender.id,
        candidate_recipients=candidate_recipients,
        message_type_value=payload.type.value,
    )
    if bypasses_delivery_blocks(canonical_type):
        delivered_owner_ids = list(candidate_recipients)
        blocked_owner_ids = []
    fanout_meta["resolved_x"] = len(delivered_owner_ids)

    priority = type_priority(canonical_type)
    response_tracking = enables_response_tracking(canonical_type)

    metadata = {
        "hid": payload.hid,
        "tt": payload.tt.isoformat(),
        "msg": payload.msg,
        "position": position_for_metadata,
        "city": payload.city,
        "province": payload.province,
        "country": payload.country,
        "delivered_owner_ids": delivered_owner_ids,
        "blocked_owner_ids": blocked_owner_ids,
        "zone_ids": zone_ids,
        "to": payload.to,
        "co": payload.co,
        "fanout": fanout_meta,
        "priority": priority.value,
        "response_tracking_enabled": response_tracking,
    }

    event = ZoneMessageEvent(
        zone_id=(zone_ids[0] if zone_ids else sender.zone_id),
        sender_id=sender.id,
        receiver_id=payload.receiver_owner_id,
        type=canonical_type.value,
        category=type_category(canonical_type),
        scope=scope,
        text=str(payload.msg.get("description") or payload.msg.get("title") or payload.type.value),
        body_json=payload.msg,
        metadata_json=metadata,
        created_at=datetime.utcnow(),
    )
    db.add(event)
    db.flush()
    db.refresh(event)

    if priority == MessagePriority.MAX:
        _log_emergency_event(
            db,
            event=event,
            canonical_type=canonical_type,
            recipient_count=len(delivered_owner_ids),
            position=position_for_metadata,
        )

    return {
        "id": event.id,
        "sender_id": sender.id,
        "receiver_id": event.receiver_id,
        "zone_id": event.zone_id,
        "type": event.type,
        "category": event.category.value,
        "scope": event.scope.value,
        "zone_ids": zone_ids,
        "delivered_owner_ids": delivered_owner_ids,
        "blocked_owner_ids": blocked_owner_ids,
        "created_at": event.created_at.isoformat(),
        "text": event.text,
        "metadata": metadata,
        "priority": priority.value,
        "response_tracking_enabled": response_tracking,
        "fanout": fanout_meta,
        "skipped": False,
    }
