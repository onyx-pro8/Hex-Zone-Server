"""CRUD operations for zone messages."""
from typing import Optional
from sqlalchemy import and_, or_
from sqlalchemy.future import select
from sqlalchemy.orm import Session, aliased
from app.models import Message, Owner
from app.models.message import MessageVisibility
from app.schemas.schemas import ZoneMessageCreate
from app.domain.message_types import normalize_message_type, type_scope, MessageScope


def create_message(db: Session, sender_id: int, payload: ZoneMessageCreate) -> Message:
    """Create a new message."""
    canonical_type = normalize_message_type(payload.type or "")
    derived_scope = type_scope(canonical_type)
    db_message = Message(
        sender_id=sender_id,
        receiver_id=payload.receiver_id,
        visibility=MessageVisibility(derived_scope.value),
        scope=MessageScope(derived_scope.value),
        message_type=canonical_type.value,
        message=payload.message,
    )
    db.add(db_message)
    db.flush()
    db.refresh(db_message)
    return db_message


def list_visible_messages(
    db: Session,
    owner_id: int,
    other_owner_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
) -> list[Message]:
    """List visible messages for an owner within the same zone."""
    sender_owner = aliased(Owner)
    owner_zone_subquery = select(Owner.zone_id).where(Owner.id == owner_id).scalar_subquery()

    visibility_filter = or_(
        Message.visibility == MessageVisibility.PUBLIC,
        and_(
            Message.visibility == MessageVisibility.PRIVATE,
            or_(Message.sender_id == owner_id, Message.receiver_id == owner_id),
        ),
    )

    query = (
        select(Message)
        .join(sender_owner, sender_owner.id == Message.sender_id)
        .where(sender_owner.zone_id == owner_zone_subquery)
        .where(visibility_filter)
        .where(Message.is_template.is_(False))
        .order_by(Message.created_at.desc())
        .offset(skip)
        .limit(limit)
    )

    if other_owner_id is not None:
        query = query.where(
            or_(
                and_(
                    Message.visibility == MessageVisibility.PUBLIC,
                    Message.sender_id.in_([owner_id, other_owner_id]),
                ),
                and_(
                    Message.visibility == MessageVisibility.PRIVATE,
                    or_(
                        and_(Message.sender_id == owner_id, Message.receiver_id == other_owner_id),
                        and_(Message.sender_id == other_owner_id, Message.receiver_id == owner_id),
                    ),
                ),
            )
        )

    result = db.execute(query)
    return result.scalars().all()
