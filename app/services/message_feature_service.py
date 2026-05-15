"""Geo propagation message workflows."""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Owner, ZoneMembership, ZoneMessageEvent
from app.services import message_block_service
from app.schemas.message_feature import MessageFeatureType, PropagationMessageCreate
from app.services.access_policy import visible_owner_ids
from app.services.geospatial_service import evaluate_member_zones
from app.domain.message_types import (
    CanonicalMessageType,
    MessageScope,
    normalize_message_type,
    type_category,
    type_scope,
)


def _to_canonical_type(message_type: MessageFeatureType) -> CanonicalMessageType:
    return normalize_message_type(message_type.value)


def create_geo_propagated_message(db: Session, sender: Owner, payload: PropagationMessageCreate) -> dict:
    canonical_type = _to_canonical_type(payload.type)
    scope = type_scope(canonical_type)
    candidate_owner_ids = visible_owner_ids(db, sender, include_inactive=False)
    zone_ids = evaluate_member_zones(
        db,
        payload.position.latitude,
        payload.position.longitude,
        candidate_owner_ids,
    )

    if scope == MessageScope.PRIVATE and payload.receiver_owner_id is None:
        raise ValueError("receiver_owner_id is required for private-scope message types")

    if payload.receiver_owner_id:
        candidate_recipients = [payload.receiver_owner_id]
    else:
        member_rows = (
            db.query(ZoneMembership.owner_id)
            .filter(ZoneMembership.zone_id.in_(zone_ids))
            .distinct()
            .all()
        )
        candidate_recipients = [row[0] for row in member_rows]
        if sender.id not in candidate_recipients:
            candidate_recipients.append(sender.id)

    delivered_owner_ids: list[int] = []
    blocked_owner_ids: list[int] = []
    for owner_id in candidate_recipients:
        if message_block_service.is_delivery_blocked(
            db,
            recipient_owner_id=owner_id,
            sender_owner_id=sender.id,
            message_type=payload.type.value,
        ):
            blocked_owner_ids.append(owner_id)
            continue
        delivered_owner_ids.append(owner_id)

    metadata = {
        "hid": payload.hid,
        "tt": payload.tt.isoformat(),
        "msg": payload.msg,
        "position": {
            "latitude": payload.position.latitude,
            "longitude": payload.position.longitude,
        },
        "city": payload.city,
        "province": payload.province,
        "country": payload.country,
        "delivered_owner_ids": delivered_owner_ids,
        "blocked_owner_ids": blocked_owner_ids,
        "zone_ids": zone_ids,
        "to": payload.to,
        "co": payload.co,
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
        "type": event.type,
        "category": event.category.value,
        "scope": event.scope.value,
        "zone_ids": zone_ids,
        "delivered_owner_ids": delivered_owner_ids,
        "blocked_owner_ids": blocked_owner_ids,
        "created_at": event.created_at.isoformat(),
        "metadata": metadata,
    }
