"""Notify existing zone members when a new user joins the account."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.crud import message as message_crud
from app.domain.message_types import CanonicalMessageType, type_category, type_scope
from app.models import Owner
from app.models.owner import OwnerRole
from app.schemas.schemas import MessageVisibilityEnum, ZoneMessageCreate, ZoneMessageResponse
from app.services.access_policy import account_root_id, messaging_visible_owner_ids
from app.websocket.manager import ws_manager

logger = logging.getLogger(__name__)

DEFAULT_MEMBER_JOIN_WELCOME = "Welcome! {member_name} has joined the zone."


def render_member_join_welcome(new_owner: Owner, template: str = DEFAULT_MEMBER_JOIN_WELCOME) -> str:
    """Replace `{first_name}`, `{last_name}`, and `{member_name}` placeholders."""
    member_name = f"{new_owner.first_name} {new_owner.last_name}".strip()
    return (
        template.replace("{first_name}", new_owner.first_name or "")
        .replace("{last_name}", new_owner.last_name or "")
        .replace("{member_name}", member_name)
    )


def _resolve_account_admin(db: Session, new_owner: Owner) -> Owner | None:
    admin = db.get(Owner, account_root_id(new_owner))
    if admin is None or admin.role != OwnerRole.ADMINISTRATOR:
        return None
    return admin


def _recipient_owner_ids(db: Session, *, admin: Owner, new_owner: Owner) -> list[int]:
    visible = messaging_visible_owner_ids(db, admin, require_same_zone=True)
    return sorted({oid for oid in visible if oid != new_owner.id})


def _message_to_response(db_message, *, zone_id: str, sender: Owner) -> ZoneMessageResponse:
    canonical_type = CanonicalMessageType.SERVICE
    return ZoneMessageResponse(
        id=db_message.id,
        zone_id=zone_id,
        sender_id=db_message.sender_id,
        receiver_id=db_message.receiver_id,
        broadcast_name=sender.message_display_name,
        type=db_message.message_type,
        category=type_category(canonical_type).value,
        scope=type_scope(canonical_type).value,
        visibility=MessageVisibilityEnum(db_message.visibility.value),
        message=db_message.message,
        created_at=db_message.created_at,
    )


async def notify_members_of_new_join(db: Session, new_owner: Owner) -> None:
    """Post a zone-wide SERVICE welcome and push it to existing members over WebSocket."""
    if new_owner.role != OwnerRole.USER:
        return

    admin = _resolve_account_admin(db, new_owner)
    if admin is None:
        return

    recipient_ids = _recipient_owner_ids(db, admin=admin, new_owner=new_owner)
    if not recipient_ids:
        return

    welcome_text = render_member_join_welcome(new_owner)
    payload = ZoneMessageCreate(message=welcome_text, type=CanonicalMessageType.SERVICE.value)
    db_message = message_crud.create_message(db, sender_id=admin.id, payload=payload)
    db.commit()

    response = _message_to_response(db_message, zone_id=admin.zone_id, sender=admin)
    ws_payload = response.model_dump(mode="json")
    await ws_manager.broadcast_to_users(recipient_ids, "NEW_MESSAGE", ws_payload)
    logger.info(
        "Member join welcome sent: new_owner_id=%s zone_id=%s recipients=%s",
        new_owner.id,
        admin.zone_id,
        recipient_ids,
    )
