"""Canonical message type taxonomy and normalization helpers."""
from __future__ import annotations

from enum import Enum


class MessageCategory(str, Enum):
    ALARM = "Alarm"
    ALERT = "Alert"
    ACCESS = "Access"


class MessageScope(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"


class MessagePriority(str, Enum):
    """Delivery priority for geo-propagated message types."""

    MAX = "MAX"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class CanonicalMessageType(str, Enum):
    SENSOR = "SENSOR"
    PANIC = "PANIC"
    NS_PANIC = "NS_PANIC"
    UNKNOWN = "UNKNOWN"
    PRIVATE = "PRIVATE"
    PA = "PA"
    SERVICE = "SERVICE"
    WELLNESS_CHECK = "WELLNESS_CHECK"
    PERMISSION = "PERMISSION"
    CHAT = "CHAT"


TYPE_PRIORITY_MAP: dict[CanonicalMessageType, MessagePriority] = {
    CanonicalMessageType.PANIC: MessagePriority.MAX,
    CanonicalMessageType.NS_PANIC: MessagePriority.MAX,
    CanonicalMessageType.UNKNOWN: MessagePriority.HIGH,
    CanonicalMessageType.WELLNESS_CHECK: MessagePriority.HIGH,
    CanonicalMessageType.SENSOR: MessagePriority.MEDIUM,
    CanonicalMessageType.PRIVATE: MessagePriority.MEDIUM,
    CanonicalMessageType.PA: MessagePriority.MEDIUM,
    CanonicalMessageType.SERVICE: MessagePriority.LOW,
    CanonicalMessageType.PERMISSION: MessagePriority.MEDIUM,
    CanonicalMessageType.CHAT: MessagePriority.MEDIUM,
}

# Message types restricted to administrators on member clients (none currently).
ADMIN_ONLY_SEND_TYPES: frozenset[CanonicalMessageType] = frozenset()

# Emergency types bypass member block filters so zone-wide distress reaches everyone.
BLOCK_BYPASS_TYPES: frozenset[CanonicalMessageType] = frozenset(
    {CanonicalMessageType.PANIC, CanonicalMessageType.NS_PANIC}
)

# Types that enable recipient acknowledgement tracking (wellness checks).
RESPONSE_TRACKING_TYPES: frozenset[CanonicalMessageType] = frozenset(
    {CanonicalMessageType.WELLNESS_CHECK}
)

TYPE_CATEGORY_MAP: dict[CanonicalMessageType, MessageCategory] = {
    CanonicalMessageType.SENSOR: MessageCategory.ALARM,
    CanonicalMessageType.PANIC: MessageCategory.ALARM,
    CanonicalMessageType.NS_PANIC: MessageCategory.ALARM,
    CanonicalMessageType.UNKNOWN: MessageCategory.ALARM,
    CanonicalMessageType.PRIVATE: MessageCategory.ALERT,
    CanonicalMessageType.PA: MessageCategory.ALERT,
    CanonicalMessageType.SERVICE: MessageCategory.ALERT,
    CanonicalMessageType.WELLNESS_CHECK: MessageCategory.ALARM,
    CanonicalMessageType.PERMISSION: MessageCategory.ACCESS,
    CanonicalMessageType.CHAT: MessageCategory.ACCESS,
}

PRIVATE_SCOPE_TYPES: set[CanonicalMessageType] = {
    CanonicalMessageType.PRIVATE,
    CanonicalMessageType.PERMISSION,
    CanonicalMessageType.CHAT,
}

TYPE_ALIAS_MAP: dict[str, CanonicalMessageType] = {
    "NS PANIC": CanonicalMessageType.NS_PANIC,
    "NS-PANIC": CanonicalMessageType.NS_PANIC,
    "WELLNESS CHECK": CanonicalMessageType.WELLNESS_CHECK,
    "WELLNESS-CHECK": CanonicalMessageType.WELLNESS_CHECK,
    # Legacy compatibility path from old contract clients.
    "NORMAL": CanonicalMessageType.SERVICE,
}

LEGACY_VISIBILITY_TO_TYPE: dict[str, CanonicalMessageType] = {
    "private": CanonicalMessageType.PRIVATE,
    "public": CanonicalMessageType.SERVICE,
}


def normalize_message_type(value: str | CanonicalMessageType) -> CanonicalMessageType:
    if isinstance(value, CanonicalMessageType):
        return value

    normalized = value.strip().upper().replace("-", "_")
    if normalized in CanonicalMessageType.__members__:
        return CanonicalMessageType[normalized]

    alias_key = value.strip().upper()
    alias = TYPE_ALIAS_MAP.get(alias_key)
    if alias:
        return alias
    raise ValueError(f"Unsupported message type: {value}")


def type_scope(message_type: CanonicalMessageType) -> MessageScope:
    return MessageScope.PRIVATE if message_type in PRIVATE_SCOPE_TYPES else MessageScope.PUBLIC


def type_category(message_type: CanonicalMessageType) -> MessageCategory:
    return TYPE_CATEGORY_MAP[message_type]


def type_priority(message_type: CanonicalMessageType) -> MessagePriority:
    return TYPE_PRIORITY_MAP.get(message_type, MessagePriority.MEDIUM)


def requires_admin_to_send(message_type: CanonicalMessageType) -> bool:
    return message_type in ADMIN_ONLY_SEND_TYPES


def bypasses_delivery_blocks(message_type: CanonicalMessageType) -> bool:
    return message_type in BLOCK_BYPASS_TYPES


def enables_response_tracking(
    message_type: CanonicalMessageType,
    *,
    sender_hid: str | None = None,
) -> bool:
    """Wellness acknowledgements only when the check was sent from a smart-home device."""
    if message_type not in RESPONSE_TRACKING_TYPES:
        return False
    normalized_hid = (sender_hid or "").strip().upper()
    if not normalized_hid:
        return False
    return not normalized_hid.startswith(("MOB-", "WEB-"))


def is_smart_home_sender_hid(sender_hid: str | None) -> bool:
    normalized_hid = (sender_hid or "").strip().upper()
    if not normalized_hid:
        return False
    return not normalized_hid.startswith(("MOB-", "WEB-"))


# Alarm types that trigger mobile push (FCM/APNS) in addition to WebSocket fan-out.
ALARM_PUSH_TYPES: frozenset[CanonicalMessageType] = frozenset(
    {
        CanonicalMessageType.UNKNOWN,
        CanonicalMessageType.PANIC,
        CanonicalMessageType.NS_PANIC,
        CanonicalMessageType.SENSOR,
    }
)

# Geo-propagated alert types that should also wake the recipient on mobile.
# Alarm + Alert categories together cover all GPS-propagated, non-access messages.
PUSHABLE_GEO_TYPES: frozenset[CanonicalMessageType] = ALARM_PUSH_TYPES | frozenset(
    {
        CanonicalMessageType.PRIVATE,
        CanonicalMessageType.PA,
        CanonicalMessageType.SERVICE,
        CanonicalMessageType.WELLNESS_CHECK,
    }
)


def is_alarm_push_type(message_type: CanonicalMessageType | str) -> bool:
    if isinstance(message_type, CanonicalMessageType):
        return message_type in ALARM_PUSH_TYPES
    return normalize_message_type(message_type) in ALARM_PUSH_TYPES


def is_pushable_geo_type(message_type: CanonicalMessageType | str) -> bool:
    """True for geo-propagated messages that should also trigger a mobile push."""
    if isinstance(message_type, CanonicalMessageType):
        return message_type in PUSHABLE_GEO_TYPES
    try:
        return normalize_message_type(message_type) in PUSHABLE_GEO_TYPES
    except ValueError:
        return False
