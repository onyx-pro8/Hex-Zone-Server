"""Geo propagation message workflows."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, or_
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
from app.models import EmergencyEvent, GuestAccessSession, Owner, ZoneMessageEvent
from app.schemas.message_feature import MessageFeatureType, PropagationMessageCreate
from app.services import message_block_service
from app.services.geospatial_service import (
    evaluate_zone_records_containing_point,
    zone_ids_for_zone_records,
)
from app.domain.service_pa_topics import (
    display_text_for_service_pa,
    validate_service_pa_message_fields,
)
from app.services.unknown_fanout_service import (
    UNKNOWN_RATE_LIMIT_SECONDS,
    resolve_nearest_owner_ids,
    unknown_fanout_limit,
)
from app.services.member_service import (
    get_owner_live_coordinates,
    set_member_live_position,
    upsert_member_location,
)
from app.services.message_relevant_zone_service import attach_relevant_zone_metadata
from app.services.network_zone_propagation import (
    resolve_network_administrator,
    resolve_network_geo_propagation_recipients,
    expand_primary_zone_gps_alert_recipients,
)
from app.services.private_plus_messaging import (
    apply_private_plus_network_shared_recipients,
    is_private_plus_network_account,
    resolve_private_plus_network_member_owner_ids,
)
from app.services.owner_home_service import (
    apply_owner_home_geocode,
    get_owner_home_coordinates,
    sync_owner_home_from_address,
)

logger = logging.getLogger(__name__)

PRIVATE_SEARCH_MIN_QUERY_LEN = 2
PRIVATE_SEARCH_MAX_RESULTS = 20

REGISTERED_ADDRESS_GEO_TYPES: frozenset[CanonicalMessageType] = frozenset(
    {
        CanonicalMessageType.SENSOR,
        CanonicalMessageType.WELLNESS_CHECK,
    }
)


class UnknownRateLimitError(Exception):
    """Raised when a sender posts UNKNOWN more than once per rate-limit window."""


class SensorRateLimitError(Exception):
    """Raised when a sender posts SENSOR telemetry faster than the throttle window."""


class PrivateScopeRecipientError(ValueError):
    """Raised when a PRIVATE receiver is not reachable for the sender's zone context."""


class GeoMessageSkipped(Exception):
    """No-op path (e.g. UNKNOWN with no usable origin coordinates)."""

    def __init__(self, detail: dict):
        self.detail = detail


def _resolve_unknown_origin(
    db: Session,
    sender: Owner,
    payload: PropagationMessageCreate,
) -> tuple[float, float, str]:
    """Origin for UNKNOWN nearest-neighbour fan-out (live message GPS, else member_locations)."""
    try:
        msg_lat = float(payload.position.latitude)
        msg_lon = float(payload.position.longitude)
        return msg_lat, msg_lon, "message_position"
    except (TypeError, ValueError):
        pass
    live = get_owner_live_coordinates(db, sender.id)
    if live is not None:
        return live[0], live[1], "member_location"
    raise GeoMessageSkipped(
        {
            "skipped": True,
            "reason": "no_origin_coordinates",
            "message": "UNKNOWN requires device location or a stored live position.",
        }
    )


def _resolve_registered_home_coordinates(
    db: Session,
    sender: Owner,
) -> tuple[float, float, str]:
    """Home coordinates for SENSOR / WELLNESS_CHECK — always from ``owner.address``."""
    sync_owner_home_from_address(sender)
    db.flush()
    coords = get_owner_home_coordinates(sender)
    if coords is None:
        raise GeoMessageSkipped(
            {
                "skipped": True,
                "reason": "no_registered_address",
                "message": (
                    "SENSOR and WELLNESS CHECK require a valid registered home address. "
                    "Update your address in account settings."
                ),
            }
        )
    return coords[0], coords[1], "registered_address"


def _geo_evaluation_coordinates(
    db: Session,
    sender: Owner,
    payload: PropagationMessageCreate,
    canonical_type: CanonicalMessageType,
) -> tuple[float, float, str]:
    """Coordinates used to test acceptable-zone geometry for propagation."""
    if canonical_type in REGISTERED_ADDRESS_GEO_TYPES:
        return _resolve_registered_home_coordinates(db, sender)
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


def _owner_ids_for_zone_id_labels(db: Session, zone_ids: list[str]) -> list[int]:
    """Active owners whose profile ``zone_id`` matches any of the given labels."""
    labels = [str(z).strip() for z in zone_ids if str(z).strip()]
    if not labels:
        return []
    rows = (
        db.query(Owner.id)
        .filter(Owner.active.is_(True), Owner.zone_id.in_(labels))
        .order_by(Owner.id.asc())
        .all()
    )
    return sorted({int(row[0]) for row in rows})


def resolve_geo_propagation_recipient_owner_ids(
    db: Session,
    *,
    latitude: float,
    longitude: float,
    exclude_owner_id: int | None = None,
    sender: Owner | None = None,
    network_zone_id: str | None = None,
) -> tuple[list[str], list[int], list[int], dict]:
    """Resolve geo alarm/alert recipients from acceptable-zone rules."""
    if sender is None:
        zone_record_ids = evaluate_zone_records_containing_point(db, float(latitude), float(longitude))
        zone_ids = zone_ids_for_zone_records(db, zone_record_ids)
        return zone_ids, zone_record_ids, [], {
            "strategy": "network_no_acceptable_zone",
            "sender_zone_record_ids": zone_record_ids,
            "sender_zone_ids": zone_ids,
            "recipient_owner_ids": [],
        }

    return resolve_network_geo_propagation_recipients(
        db,
        sender,
        latitude=float(latitude),
        longitude=float(longitude),
        exclude_owner_id=exclude_owner_id,
        network_zone_id=network_zone_id,
    )


def _assert_private_receiver_reachable(
    db: Session,
    sender: Owner,
    receiver_owner_id: int,
    *,
    latitude: float,
    longitude: float,
    sender_zone_record_ids: list[int],
    network_zone_id: str | None = None,
) -> None:
    """PRIVATE: sender must be inside a zone; receiver must be in the PANIC/PA pool."""
    receiver = db.get(Owner, receiver_owner_id)
    if receiver is None or not bool(receiver.active):
        raise PrivateScopeRecipientError(
            "PRIVATE receiver must be an active member."
        )
    if receiver.id == sender.id:
        raise PrivateScopeRecipientError(
            "PRIVATE messages cannot target yourself."
        )

    if not sender_zone_record_ids:
        raise PrivateScopeRecipientError(
            "You are not currently inside any zone, so you cannot send a PRIVATE message."
        )

    _, _, pool, _ = resolve_geo_propagation_recipient_owner_ids(
        db,
        latitude=latitude,
        longitude=longitude,
        exclude_owner_id=None,
        sender=sender,
        network_zone_id=network_zone_id,
    )
    if is_private_plus_network_account(db, sender):
        pool = sorted(
            set(pool)
            | set(
                resolve_private_plus_network_member_owner_ids(
                    db,
                    sender,
                    exclude_owner_id=None,
                )
            )
        )
    if receiver.id not in pool:
        raise PrivateScopeRecipientError(
            "PRIVATE receiver must be reachable under current primary/secondary zone routing."
        )


def _resolve_private_location_status(
    db: Session,
    sender: Owner,
    *,
    latitude: float | None,
    longitude: float | None,
    network_zone_id: str | None = None,
) -> str:
    """Location gate for PRIVATE compose: coordinates, then acceptable-zone geometry."""
    if latitude is None or longitude is None:
        return "no_coordinates"
    _, zone_record_ids, _, _ = resolve_geo_propagation_recipient_owner_ids(
        db,
        latitude=float(latitude),
        longitude=float(longitude),
        exclude_owner_id=sender.id,
        sender=sender,
        network_zone_id=network_zone_id,
    )
    if not zone_record_ids:
        return "outside_zone"
    return "inside_zone"


def _private_search_members(
    db: Session,
    candidate_ids: list[int],
    query: str,
    *,
    limit: int = PRIVATE_SEARCH_MAX_RESULTS,
) -> list[dict]:
    q = (query or "").strip()
    if not candidate_ids:
        return []

    cap = max(1, min(int(limit), PRIVATE_SEARCH_MAX_RESULTS))
    query_builder = (
        db.query(Owner)
        .filter(
            Owner.id.in_(candidate_ids),
            Owner.active.is_(True),
        )
        .order_by(Owner.first_name.asc(), Owner.last_name.asc(), Owner.id.asc())
    )
    if len(q) >= PRIVATE_SEARCH_MIN_QUERY_LEN:
        pattern = f"%{q.lower()}%"
        full_name = func.lower(
            func.trim(func.concat(func.coalesce(Owner.first_name, ""), " ", func.coalesce(Owner.last_name, "")))
        )
        query_builder = query_builder.filter(
            or_(
                func.lower(Owner.broadcast_name).like(pattern),
                func.lower(Owner.first_name).like(pattern),
                func.lower(Owner.last_name).like(pattern),
                func.lower(Owner.email).like(pattern),
                full_name.like(pattern),
            )
        )
    elif len(q) > 0:
        return []
    rows = query_builder.limit(cap).all()

    return [
        {
            "id": int(row.id),
            "display_name": row.message_display_name,
            "broadcast_name": (row.broadcast_name or "").strip() or None,
            "first_name": row.first_name,
            "last_name": row.last_name,
            "email": row.email,
            "zone_id": row.zone_id,
            "subtitle": row.email,
        }
        for row in rows
    ]


def search_private_message_recipients(
    db: Session,
    sender: Owner,
    query: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    limit: int = PRIVATE_SEARCH_MAX_RESULTS,
) -> dict:
    """Search network members by name or email for PRIVATE compose (invited members only)."""
    live = get_owner_live_coordinates(db, sender.id)
    lat = latitude if latitude is not None else (live[0] if live else None)
    lon = longitude if longitude is not None else (live[1] if live else None)
    location_status = _resolve_private_location_status(
        db,
        sender,
        latitude=lat,
        longitude=lon,
    )
    if location_status != "inside_zone":
        return {"zone_ids": [], "members": [], "location_status": location_status}

    zone_ids, zone_record_ids, candidate_ids, _ = resolve_geo_propagation_recipient_owner_ids(
        db,
        latitude=float(lat),
        longitude=float(lon),
        exclude_owner_id=sender.id,
        sender=sender,
    )
    if is_private_plus_network_account(db, sender):
        candidate_ids = sorted(
            set(candidate_ids)
            | set(
                resolve_private_plus_network_member_owner_ids(
                    db,
                    sender,
                    exclude_owner_id=sender.id,
                )
            )
        )
    members = _private_search_members(db, candidate_ids, query, limit=limit)
    return {"zone_ids": zone_ids, "members": members, "location_status": "inside_zone"}


def search_network_guest_private_message_recipients(
    db: Session,
    *,
    guest_session: GuestAccessSession,
    query: str,
    latitude: float | None = None,
    longitude: float | None = None,
    limit: int = PRIVATE_SEARCH_MAX_RESULTS,
) -> dict:
    """PRIVATE recipient search for users not in a network (network-access guest session)."""
    network_id = (guest_session.zone_id or "").strip()
    admin = resolve_network_administrator(db, network_id)
    if admin is None:
        return {"zone_ids": [], "members": [], "location_status": "not_in_network"}

    location_status = _resolve_private_location_status(
        db,
        admin,
        latitude=latitude,
        longitude=longitude,
        network_zone_id=network_id,
    )
    if location_status != "inside_zone":
        return {"zone_ids": [], "members": [], "location_status": location_status}

    zone_ids, _, candidate_ids, _ = resolve_geo_propagation_recipient_owner_ids(
        db,
        latitude=float(latitude),
        longitude=float(longitude),
        exclude_owner_id=None,
        sender=admin,
        network_zone_id=network_id,
    )
    members = _private_search_members(db, candidate_ids, query, limit=limit)
    return {"zone_ids": zone_ids, "members": members, "location_status": "inside_zone"}


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
    *,
    exclude_sender_from_recipients: bool = True,
    network_zone_id: str | None = None,
) -> tuple[list[str], list[int], dict]:
    """Resolve recipients for geo alarm/alert types (not UNKNOWN).

    Primary acceptable zone → administrator + invited account members, plus for
    PANIC / NS_PANIC / PA / SERVICE every owner GPS-located inside that primary zone.
    Secondary acceptable zone → zone creator only.
    Outside both → no recipients.
    """
    eval_lat, eval_lon, geo_source = _geo_evaluation_coordinates(
        db, sender, payload, canonical_type
    )
    sender_zone_ids, sender_zone_record_ids, recipient_owner_ids, zone_meta = (
        resolve_geo_propagation_recipient_owner_ids(
            db,
            latitude=eval_lat,
            longitude=eval_lon,
            exclude_owner_id=sender.id if exclude_sender_from_recipients else None,
            sender=sender,
            network_zone_id=network_zone_id,
        )
    )
    zone_meta = {**zone_meta, "geo_evaluation_source": geo_source}

    recipient_owner_ids, zone_meta = apply_private_plus_network_shared_recipients(
        db,
        sender=sender,
        message_type=canonical_type,
        sender_zone_record_ids=sender_zone_record_ids,
        recipient_owner_ids=recipient_owner_ids,
        zone_meta=zone_meta,
        exclude_sender_id=sender.id if exclude_sender_from_recipients else None,
    )
    recipient_owner_ids, zone_meta = expand_primary_zone_gps_alert_recipients(
        db,
        message_type=canonical_type,
        recipient_owner_ids=recipient_owner_ids,
        zone_meta=zone_meta,
        exclude_sender_id=sender.id if exclude_sender_from_recipients else None,
    )

    if scope == MessageScope.PRIVATE and payload.receiver_owner_id is None:
        raise ValueError("receiver_owner_id is required for private-scope message types")

    if scope == MessageScope.PRIVATE:
        _assert_private_receiver_reachable(
            db,
            sender,
            payload.receiver_owner_id,
            latitude=eval_lat,
            longitude=eval_lon,
            sender_zone_record_ids=sender_zone_record_ids,
            network_zone_id=network_zone_id,
        )
        return (
            sender_zone_ids,
            [payload.receiver_owner_id],
            {
                **zone_meta,
                "strategy": "private_sender_in_zone",
            },
        )

    return sender_zone_ids, recipient_owner_ids, zone_meta


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
    validate_service_pa_message_fields(canonical_type, payload.msg)
    scope = type_scope(canonical_type)
    account_type = str(sender.account_type.value).strip().lower()

    fanout_meta: dict = {
        "account_type": account_type,
    }

    if canonical_type == CanonicalMessageType.UNKNOWN:
        _assert_unknown_rate_limit_ok(db, sender.id)
        try:
            origin_lat, origin_lon, origin_source = _resolve_unknown_origin(db, sender, payload)
        except GeoMessageSkipped as exc:
            raise exc
        if origin_source == "message_position":
            set_member_live_position(db, sender.id, origin_lat, origin_lon)

        target_x = unknown_fanout_limit(sender)
        candidate_recipients = resolve_nearest_owner_ids(
            db,
            origin_lat=origin_lat,
            origin_lon=origin_lon,
            sender_id=sender.id,
            limit=target_x,
        )
        zone_ids: list[str] = []
        fanout_meta = {
            "account_type": account_type,
            "strategy": "unknown_nearest_global",
            "target_x": target_x,
            "resolved_x": len(candidate_recipients),
            "origin": {
                "latitude": origin_lat,
                "longitude": origin_lon,
                "source": origin_source,
            },
        }
        candidate_recipients, fanout_meta = apply_private_plus_network_shared_recipients(
            db,
            sender=sender,
            message_type=canonical_type,
            sender_zone_record_ids=[],
            recipient_owner_ids=candidate_recipients,
            zone_meta=fanout_meta,
            exclude_sender_id=sender.id,
        )
        fanout_meta["target_x"] = len(candidate_recipients)
        position_for_metadata = {"latitude": origin_lat, "longitude": origin_lon}
    else:
        if canonical_type == CanonicalMessageType.SENSOR:
            _assert_sensor_rate_limit_ok(db, sender.id)
        zone_ids, candidate_recipients, zone_meta = _zone_based_recipients(
            db, sender, payload, canonical_type, scope
        )
        eval_lat, eval_lon, geo_source = _geo_evaluation_coordinates(
            db, sender, payload, canonical_type
        )
        position_for_metadata = {
            "latitude": eval_lat,
            "longitude": eval_lon,
            "geo_evaluation_source": geo_source,
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
    if fanout_meta.get("sender_zone_record_ids") or fanout_meta.get("recipient_zone_record_ids"):
        attach_relevant_zone_metadata(
            db,
            metadata=metadata,
            zone_meta=fanout_meta,
            delivered_owner_ids=delivered_owner_ids,
        )

    event = ZoneMessageEvent(
        zone_id=(zone_ids[0] if zone_ids else sender.zone_id),
        sender_id=sender.id,
        receiver_id=payload.receiver_owner_id,
        type=canonical_type.value,
        category=type_category(canonical_type),
        scope=scope,
        text=display_text_for_service_pa(payload.msg)
        if canonical_type in (CanonicalMessageType.PA, CanonicalMessageType.SERVICE)
        else str(payload.msg.get("description") or payload.msg.get("title") or payload.type.value),
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


def create_network_guest_geo_propagated_message(
    db: Session,
    *,
    guest_session: GuestAccessSession,
    payload: PropagationMessageCreate,
) -> dict:
    """Geo-propagated alarm/alert from a user not in a network (network-access guest after QR scan)."""
    network_id = (guest_session.zone_id or "").strip()
    admin = resolve_network_administrator(db, network_id)
    if admin is None:
        raise ValueError("No active network administrator for this network id.")

    routing_owner = admin
    canonical_type = _to_canonical_type(payload.type)
    validate_service_pa_message_fields(canonical_type, payload.msg)
    scope = type_scope(canonical_type)
    account_type = str(routing_owner.account_type.value).strip().lower()
    guest_id = (guest_session.guest_id or "").strip()

    fanout_meta: dict = {
        "account_type": account_type,
        "network_guest_id": guest_id,
        "network_guest_session_id": int(guest_session.id),
    }

    if canonical_type == CanonicalMessageType.UNKNOWN:
        _assert_unknown_rate_limit_ok(db, routing_owner.id)
        try:
            origin_lat, origin_lon, origin_source = _resolve_unknown_origin(db, routing_owner, payload)
        except GeoMessageSkipped as exc:
            raise exc

        target_x = unknown_fanout_limit(routing_owner)
        candidate_recipients = resolve_nearest_owner_ids(
            db,
            origin_lat=origin_lat,
            origin_lon=origin_lon,
            sender_id=-1,
            limit=target_x,
        )
        zone_ids: list[str] = []
        fanout_meta = {
            **fanout_meta,
            "strategy": "unknown_nearest_global",
            "target_x": target_x,
            "resolved_x": len(candidate_recipients),
            "origin": {
                "latitude": origin_lat,
                "longitude": origin_lon,
                "source": origin_source,
            },
        }
        candidate_recipients, fanout_meta = apply_private_plus_network_shared_recipients(
            db,
            sender=routing_owner,
            message_type=canonical_type,
            sender_zone_record_ids=[],
            recipient_owner_ids=candidate_recipients,
            zone_meta=fanout_meta,
            exclude_sender_id=None,
        )
        fanout_meta["target_x"] = len(candidate_recipients)
        position_for_metadata = {"latitude": origin_lat, "longitude": origin_lon}
    else:
        if canonical_type == CanonicalMessageType.SENSOR:
            raise ValueError("Guests may not send SENSOR messages via network access.")
        if canonical_type == CanonicalMessageType.WELLNESS_CHECK:
            raise ValueError("Guests may not send WELLNESS CHECK messages via network access.")
        zone_ids, candidate_recipients, zone_meta = _zone_based_recipients(
            db,
            routing_owner,
            payload,
            canonical_type,
            scope,
            exclude_sender_from_recipients=False,
            network_zone_id=network_id,
        )
        eval_lat, eval_lon, geo_source = _geo_evaluation_coordinates(
            db, routing_owner, payload, canonical_type
        )
        position_for_metadata = {
            "latitude": eval_lat,
            "longitude": eval_lon,
            "geo_evaluation_source": geo_source,
        }
        fanout_meta = {
            **fanout_meta,
            **zone_meta,
            "target_x": len(candidate_recipients),
        }

    delivered_owner_ids, blocked_owner_ids = _apply_delivery_blocks(
        db,
        sender_id=routing_owner.id,
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
        "flow": "network_guest_geo",
        "guest_id": guest_id,
        "guest_name": guest_session.guest_name,
        "network_zone_id": network_id,
    }
    if fanout_meta.get("sender_zone_record_ids") or fanout_meta.get("recipient_zone_record_ids"):
        attach_relevant_zone_metadata(
            db,
            metadata=metadata,
            zone_meta=fanout_meta,
            delivered_owner_ids=delivered_owner_ids,
        )

    event = ZoneMessageEvent(
        zone_id=(zone_ids[0] if zone_ids else network_id),
        sender_id=None,
        sender_guest_id=guest_id or None,
        guest_access_session_id=guest_session.id,
        receiver_id=payload.receiver_owner_id,
        type=canonical_type.value,
        category=type_category(canonical_type),
        scope=scope,
        text=display_text_for_service_pa(payload.msg)
        if canonical_type in (CanonicalMessageType.PA, CanonicalMessageType.SERVICE)
        else str(payload.msg.get("description") or payload.msg.get("title") or payload.type.value),
        body_json={**payload.msg, "guest_id": guest_id, "guest_name": guest_session.guest_name},
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
        "sender_id": None,
        "sender_guest_id": guest_id,
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
