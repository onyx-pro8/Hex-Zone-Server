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


TYPE_CATEGORY_MAP: dict[CanonicalMessageType, MessageCategory] = {
    CanonicalMessageType.SENSOR: MessageCategory.ALARM,
    CanonicalMessageType.PANIC: MessageCategory.ALARM,
    CanonicalMessageType.NS_PANIC: MessageCategory.ALARM,
    CanonicalMessageType.UNKNOWN: MessageCategory.ALARM,
    CanonicalMessageType.PRIVATE: MessageCategory.ALERT,
    CanonicalMessageType.PA: MessageCategory.ALERT,
    CanonicalMessageType.SERVICE: MessageCategory.ALERT,
    CanonicalMessageType.WELLNESS_CHECK: MessageCategory.ALERT,
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


# Alarm types that trigger mobile push (FCM/APNS) in addition to WebSocket fan-out.
ALARM_PUSH_TYPES: frozenset[CanonicalMessageType] = frozenset(
    {
        CanonicalMessageType.UNKNOWN,
        CanonicalMessageType.PANIC,
        CanonicalMessageType.NS_PANIC,
        CanonicalMessageType.SENSOR,
    }
)


def is_alarm_push_type(message_type: CanonicalMessageType | str) -> bool:
    if isinstance(message_type, CanonicalMessageType):
        return message_type in ALARM_PUSH_TYPES
    return normalize_message_type(message_type) in ALARM_PUSH_TYPES
