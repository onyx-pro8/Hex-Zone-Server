"""Private+ (family) network-shared messaging for selected alert types."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.message_types import CanonicalMessageType, normalize_message_type
from app.models import Owner
from app.models.owner import AccountType
from app.services.access_policy import account_propagation_owner_ids, account_root_id
from app.services.network_zone_propagation import (
    owner_participates_in_network,
    resolve_network_administrator,
)
from app.services.registration_code_service import (
    PRICING_TIER_PRIVATE_PLUS,
    normalize_pricing_tier_key,
)

PRIVATE_PLUS_NETWORK_SHARED_MESSAGE_TYPES: frozenset[CanonicalMessageType] = frozenset(
    {
        CanonicalMessageType.PANIC,
        CanonicalMessageType.NS_PANIC,
        CanonicalMessageType.PA,
        CanonicalMessageType.SERVICE,
    }
)


def is_private_plus_account(owner: Owner) -> bool:
    return normalize_pricing_tier_key(owner.account_type.value) == PRICING_TIER_PRIVATE_PLUS


def is_private_plus_network_shared_message_type(
    message_type: CanonicalMessageType | str,
) -> bool:
    if isinstance(message_type, CanonicalMessageType):
        return message_type in PRIVATE_PLUS_NETWORK_SHARED_MESSAGE_TYPES
    try:
        return normalize_message_type(message_type) in PRIVATE_PLUS_NETWORK_SHARED_MESSAGE_TYPES
    except ValueError:
        return False


def is_private_plus_network_account(db: Session, owner: Owner) -> bool:
    """True when the account root (administrator tier) is Private+."""
    root = db.get(Owner, account_root_id(owner))
    return root is not None and is_private_plus_account(root)


def private_plus_network_shared_delivery_applies(
    db: Session,
    sender: Owner,
    message_type: CanonicalMessageType,
) -> bool:
    if not is_private_plus_network_shared_message_type(message_type):
        return False
    if not is_private_plus_network_account(db, sender):
        return False
    return owner_participates_in_network(db, sender)


def apply_private_plus_network_shared_recipients(
    db: Session,
    *,
    sender: Owner,
    message_type: CanonicalMessageType,
    sender_zone_record_ids: list[int],
    recipient_owner_ids: list[int],
    zone_meta: dict,
    exclude_sender_id: int | None,
) -> tuple[list[int], dict]:
    """Expand delivery to the full Private+ network when sender is inside any acceptable zone.

    SENSOR, WELLNESS_CHECK, PRIVATE, UNKNOWN, and ACCESS types are unchanged.
    """
    if not private_plus_network_shared_delivery_applies(db, sender, message_type):
        return recipient_owner_ids, zone_meta
    if not sender_zone_record_ids:
        return recipient_owner_ids, zone_meta

    network_id = (sender.zone_id or "").strip()
    admin = resolve_network_administrator(db, network_id)
    if admin is None:
        return recipient_owner_ids, zone_meta

    pool = set(account_propagation_owner_ids(db, admin))
    if exclude_sender_id is not None:
        pool.discard(int(exclude_sender_id))
    sorted_recipients = sorted(pool)
    return sorted_recipients, {
        **zone_meta,
        "strategy": "private_plus_network_shared",
        "private_plus_network_shared": True,
        "recipient_owner_ids": sorted_recipients,
    }


def geo_event_visible_in_private_plus_shared_inbox(
    db: Session,
    viewer: Owner,
    *,
    sender_id: int | None,
    message_type: str,
) -> bool:
    """Inbox mirror: Private+ members see network-shared types from same-account senders."""
    if not is_private_plus_network_account(db, viewer):
        return False
    if not is_private_plus_network_shared_message_type(message_type):
        return False
    if sender_id is None:
        return False
    sender = db.get(Owner, int(sender_id))
    if sender is None or not bool(sender.active):
        return False
    if account_root_id(viewer) != account_root_id(sender):
        return False
    viewer_network = (viewer.zone_id or "").strip()
    sender_network = (sender.zone_id or "").strip()
    return bool(viewer_network) and viewer_network == sender_network


def account_type_is_private_plus_value(account_type: str | AccountType) -> bool:
    raw = account_type.value if isinstance(account_type, AccountType) else str(account_type)
    return normalize_pricing_tier_key(raw) == PRICING_TIER_PRIVATE_PLUS
