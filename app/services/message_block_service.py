"""Per-member message delivery blocks (by sender and/or message type)."""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import MessageBlock


def _coerce_owner_id(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def is_delivery_blocked(
    db: Session,
    *,
    recipient_owner_id: int,
    sender_owner_id: int | None,
    message_type: str,
) -> bool:
    """True when *recipient* has a rule that suppresses this delivery.

    Rule semantics (each row is independent; any matching row blocks):

    - **Type-only** (``blocked_message_type`` set, ``blocked_owner_id`` null):
      block that type from all senders in the zone.
    - **Member-only** (``blocked_owner_id`` set, ``blocked_message_type`` null):
      block all types from that member.
    - **Both set**: block only that member's messages of that type.

    Guest-originated traffic (``sender_owner_id`` is None) matches type-only rules only.
    """
    normalized_sender = _coerce_owner_id(sender_owner_id)
    sender_clause = (
        MessageBlock.blocked_owner_id.is_(None)
        if normalized_sender is None
        else or_(
            MessageBlock.blocked_owner_id.is_(None),
            MessageBlock.blocked_owner_id == normalized_sender,
        )
    )
    type_clause = or_(
        MessageBlock.blocked_message_type.is_(None),
        MessageBlock.blocked_message_type == message_type,
    )
    return (
        db.query(MessageBlock.id)
        .filter(
            MessageBlock.owner_id == recipient_owner_id,
            sender_clause,
            type_clause,
        )
        .first()
        is not None
    )


def filter_zone_message_responses_for_viewer(
    db: Session,
    viewer_owner_id: int,
    items: list,
) -> list:
    """Drop inbox rows the viewer has blocked (by sender and/or type)."""
    visible = []
    for item in items:
        sender_id = _coerce_owner_id(getattr(item, "sender_id", None))
        msg_type = str(getattr(item, "type", "") or "")
        if is_delivery_blocked(
            db,
            recipient_owner_id=viewer_owner_id,
            sender_owner_id=sender_id,
            message_type=msg_type,
        ):
            continue
        visible.append(item)
    return visible


def find_duplicate_block(
    db: Session,
    *,
    owner_id: int,
    blocked_owner_id: int | None,
    blocked_message_type: str | None,
) -> MessageBlock | None:
    return (
        db.query(MessageBlock)
        .filter(
            MessageBlock.owner_id == owner_id,
            MessageBlock.blocked_owner_id == blocked_owner_id,
            MessageBlock.blocked_message_type == blocked_message_type,
        )
        .first()
    )
