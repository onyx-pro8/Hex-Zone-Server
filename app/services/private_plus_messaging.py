"""Private+ (family) network-shared messaging — union with standard zone routing."""
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

# Geo-propagated types that Private+ always unions with the full account network.
PRIVATE_PLUS_NETWORK_UNION_MESSAGE_TYPES: frozenset[CanonicalMessageType] = frozenset(
    {
        CanonicalMessageType.SENSOR,
        CanonicalMessageType.PANIC,
        CanonicalMessageType.NS_PANIC,
        CanonicalMessageType.UNKNOWN,
        CanonicalMessageType.PA,
        CanonicalMessageType.SERVICE,
        CanonicalMessageType.WELLNESS_CHECK,
    }
)


def is_private_plus_account(owner: Owner) -> bool:
    return normalize_pricing_tier_key(owner.account_type.value) == PRICING_TIER_PRIVATE_PLUS


def is_private_plus_network_union_message_type(
    message_type: CanonicalMessageType | str,
) -> bool:
    if isinstance(message_type, CanonicalMessageType):
        return message_type in PRIVATE_PLUS_NETWORK_UNION_MESSAGE_TYPES
    try:
        return normalize_message_type(message_type) in PRIVATE_PLUS_NETWORK_UNION_MESSAGE_TYPES
    except ValueError:
        return False


def is_private_plus_network_account(db: Session, owner: Owner) -> bool:
    """True when the account root (administrator tier) is Private+."""
    root = db.get(Owner, account_root_id(owner))
    return root is not None and is_private_plus_account(root)


def private_plus_network_delivery_applies(
    db: Session,
    sender: Owner,
    message_type: CanonicalMessageType,
) -> bool:
    if not is_private_plus_network_union_message_type(message_type):
        return False
    if not is_private_plus_network_account(db, sender):
        return False
    return owner_participates_in_network(db, sender)


def resolve_private_plus_network_member_owner_ids(
    db: Session,
    sender: Owner,
    *,
    exclude_owner_id: int | None = None,
) -> list[int]:
    """Active admin + invited members on the sender's Private+ account/network."""
    if not is_private_plus_network_account(db, sender):
        return []
    if not owner_participates_in_network(db, sender):
        return []

    network_id = (sender.zone_id or "").strip()
    if not network_id:
        return []

    admin = resolve_network_administrator(db, network_id)
    if admin is None:
        return []

    pool = set(account_propagation_owner_ids(db, admin))
    if exclude_owner_id is not None:
        pool.discard(int(exclude_owner_id))
    return sorted(pool)


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
    """Union standard routing recipients with every active Private+ network member.

    Standard zone / nearest-neighbour rules still apply; Private+ additionally reaches
    the whole account network. PRIVATE one-to-one delivery uses the expanded pool only
    for recipient validation/search — the delivered list remains a single receiver.
    """
    _ = sender_zone_record_ids  # zone geometry still recorded in zone_meta for audit
    if not private_plus_network_delivery_applies(db, sender, message_type):
        return recipient_owner_ids, zone_meta

    network_members = resolve_private_plus_network_member_owner_ids(
        db,
        sender,
        exclude_owner_id=exclude_sender_id,
    )
    if not network_members:
        return recipient_owner_ids, zone_meta

    base_strategy = zone_meta.get("strategy")
    merged = sorted(set(recipient_owner_ids) | set(network_members))
    added = sorted(set(network_members) - set(recipient_owner_ids))

    meta = {
        **zone_meta,
        "private_plus_network_shared": True,
        "private_plus_network_member_ids": network_members,
        "private_plus_network_added_owner_ids": added,
        "recipient_owner_ids": merged,
    }
    if added:
        if base_strategy:
            meta["strategy"] = f"{base_strategy}+private_plus_network"
        else:
            meta["strategy"] = "private_plus_network_shared"
    return merged, meta


def geo_event_visible_in_private_plus_shared_inbox(
    db: Session,
    viewer: Owner,
    *,
    sender_id: int | None,
    message_type: str,
) -> bool:
    """Inbox mirror: Private+ members see geo events from same-account network senders."""
    _ = message_type
    if not is_private_plus_network_account(db, viewer):
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
