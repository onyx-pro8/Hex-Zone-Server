"""Notify zone members when a new user joins the account."""

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

DEFAULT_MEMBER_JOIN_WELCOME = "Welcome! {member_name} has joined the {network_name}."


def render_member_join_welcome(
    new_owner: Owner,
    *,
    network_name: str,
    template: str | None = None,
) -> str:
    """Replace member / network placeholders in the welcome template."""
    raw = (template or "").strip() or DEFAULT_MEMBER_JOIN_WELCOME
    member_name = f"{new_owner.first_name} {new_owner.last_name}".strip()
    network = (network_name or "").strip() or (new_owner.zone_id or "").strip() or "network"
    return (
        raw.replace("{first_name}", new_owner.first_name or "")
        .replace("{last_name}", new_owner.last_name or "")
        .replace("{member_name}", member_name)
        .replace("{member name}", member_name)
        .replace("{network_name}", network)
        .replace("{network name}", network)
    )


def _resolve_account_admin(db: Session, new_owner: Owner) -> Owner | None:
    admin = db.get(Owner, account_root_id(new_owner))
    if admin is None or admin.role != OwnerRole.ADMINISTRATOR:
        return None
    return admin


def _admin_welcome_template(admin: Owner) -> str:
    configured = getattr(admin, "member_join_welcome", None)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return DEFAULT_MEMBER_JOIN_WELCOME


def _recipient_owner_ids(db: Session, *, admin: Owner, new_owner: Owner) -> list[int]:
    """Existing members plus the new joiner (so they receive the welcome toast)."""
    visible = messaging_visible_owner_ids(db, admin, require_same_zone=True)
    return sorted({oid for oid in visible} | {new_owner.id})


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


async def notify_members_of_new_join(db: Session, new_owner: Owner) -> str | None:
    """Post a zone-wide SERVICE welcome and push it to members (including the joiner).

    Returns the rendered welcome text when a message was sent, otherwise ``None``.
    """
    if new_owner.role != OwnerRole.USER:
        return None

    admin = _resolve_account_admin(db, new_owner)
    if admin is None:
        return None

    recipient_ids = _recipient_owner_ids(db, admin=admin, new_owner=new_owner)
    if not recipient_ids:
        return None

    network_name = (admin.zone_id or new_owner.zone_id or "").strip()
    welcome_text = render_member_join_welcome(
        new_owner,
        network_name=network_name,
        template=_admin_welcome_template(admin),
    )
    payload = ZoneMessageCreate(message=welcome_text, type=CanonicalMessageType.SERVICE.value)
    db_message = message_crud.create_message(db, sender_id=admin.id, payload=payload)
    db.commit()

    response = _message_to_response(db_message, zone_id=admin.zone_id, sender=admin)
    ws_payload = response.model_dump(mode="json")
    ws_payload["member_join_welcome"] = True
    await ws_manager.broadcast_to_users(recipient_ids, "NEW_MESSAGE", ws_payload)
    logger.info(
        "Member join welcome sent: new_owner_id=%s zone_id=%s recipients=%s",
        new_owner.id,
        admin.zone_id,
        recipient_ids,
    )
    return welcome_text
