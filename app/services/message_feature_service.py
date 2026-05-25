"""Geo propagation message workflows."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.message_types import (
    CanonicalMessageType,
    MessageScope,
    normalize_message_type,
    type_category,
    type_scope,
)
from app.models import Owner, ZoneMessageEvent
from app.schemas.message_feature import MessageFeatureType, PropagationMessageCreate
from app.services import message_block_service
from app.services.access_policy import visible_owner_ids
from app.services.geospatial_service import owner_ids_whose_acceptable_zones_contain_point
from app.services.unknown_fanout_service import (
    UNKNOWN_RATE_LIMIT_SECONDS,
    resolve_nearest_owner_ids,
    unknown_fanout_limit,
)


class UnknownRateLimitError(Exception):
    """Raised when a sender posts UNKNOWN more than once per rate-limit window."""


class GeoMessageSkipped(Exception):
    """No-op path (e.g. UNKNOWN with no usable origin coordinates)."""

    def __init__(self, detail: dict):
        self.detail = detail


def _merge_propagation_recipients(
    *,
    sender_id: int,
    account_owner_ids: list[int],
    acceptable_zone_owner_ids: list[int],
) -> list[int]:
    """Union same-account members with cross-account acceptable-zone owners."""
    merged = set(account_owner_ids) | set(acceptable_zone_owner_ids)
    merged.discard(sender_id)
    return sorted(merged)


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

    Delivers to:
    - all members on the sender's account, and
    - any other owner whose acceptable zone geometry contains the message point.
    """
    if scope == MessageScope.PRIVATE and payload.receiver_owner_id is None:
        raise ValueError("receiver_owner_id is required for private-scope message types")

    if payload.receiver_owner_id:
        return [], [payload.receiver_owner_id], {"strategy": "direct_receiver"}

    latitude = float(payload.position.latitude)
    longitude = float(payload.position.longitude)

    account_owner_ids = visible_owner_ids(db, sender, include_inactive=False)
    zone_ids, acceptable_zone_owner_ids = owner_ids_whose_acceptable_zones_contain_point(
        db,
        latitude,
        longitude,
    )
    candidate_recipients = _merge_propagation_recipients(
        sender_id=sender.id,
        account_owner_ids=account_owner_ids,
        acceptable_zone_owner_ids=acceptable_zone_owner_ids,
    )
    meta = {
        "strategy": "account_plus_acceptable_zone",
        "account_member_ids": account_owner_ids,
        "acceptable_zone_owner_ids": acceptable_zone_owner_ids,
    }
    return zone_ids, candidate_recipients, meta


def create_geo_propagated_message(db: Session, sender: Owner, payload: PropagationMessageCreate) -> dict:
    canonical_type = _to_canonical_type(payload.type)
    scope = type_scope(canonical_type)
    account_type = str(sender.account_type.value).strip().lower()

    fanout_meta: dict = {
        "account_type": account_type,
    }

    if canonical_type == CanonicalMessageType.UNKNOWN:
        _assert_unknown_rate_limit_ok(db, sender.id)
        origin_lat, origin_lon, origin_source = _resolve_unknown_origin(sender, payload)
        if origin_source == "message_position":
            sender.latitude = origin_lat
            sender.longitude = origin_lon
            sender.location_updated_at = datetime.utcnow()
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
            "strategy": "unknown_nearest",
            "target_x": target_x,
            "resolved_x": len(candidate_recipients),
            "origin": {
                "latitude": origin_lat,
                "longitude": origin_lon,
                "source": origin_source,
            },
        }
        position_for_metadata = {"latitude": origin_lat, "longitude": origin_lon}
    else:
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
    fanout_meta["resolved_x"] = len(delivered_owner_ids)

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

    return {
        "id": event.id,
        "sender_id": sender.id,
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
        "fanout": fanout_meta,
        "skipped": False,
    }
