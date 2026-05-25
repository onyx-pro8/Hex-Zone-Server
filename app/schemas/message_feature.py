"""Schemas for geo propagation, blocks, and permission schedules."""
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from app.domain.message_types import CanonicalMessageType, normalize_message_type


class MessageFeatureType(str, Enum):
    SENSOR = CanonicalMessageType.SENSOR.value
    PANIC = CanonicalMessageType.PANIC.value
    NS_PANIC = CanonicalMessageType.NS_PANIC.value
    UNKNOWN = CanonicalMessageType.UNKNOWN.value
    PRIVATE = CanonicalMessageType.PRIVATE.value
    PA = CanonicalMessageType.PA.value
    SERVICE = CanonicalMessageType.SERVICE.value
    WELLNESS_CHECK = CanonicalMessageType.WELLNESS_CHECK.value
    PERMISSION = CanonicalMessageType.PERMISSION.value
    CHAT = CanonicalMessageType.CHAT.value


class CoordinatePayload(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


class PropagationMessageCreate(BaseModel):
    type: MessageFeatureType
    hid: str = Field(..., min_length=1, max_length=255, description="Device / session handle.")
    tt: datetime = Field(default_factory=datetime.utcnow, description="Message time (UTC).")
    msg: dict = Field(
        default_factory=dict,
        description=(
            "Type-specific payload. For **PERMISSION**, include keys such as `guest_name`, "
            "`guest_id`, and `event_id` for schedule matching."
        ),
    )
    position: CoordinatePayload
    city: str | None = Field(default=None, max_length=120)
    province: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)
    to: str | None = Field(default=None, description="QR/access zone target")
    co: str | None = Field(default=None, description="Device zone id")
    receiver_owner_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_type_aliases(cls, data):
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        value = payload.get("type")
        if isinstance(value, str):
            payload["type"] = normalize_message_type(value).value
        return payload


class BlockRuleCreate(BaseModel):
    blocked_owner_id: int | None = Field(
        default=None,
        ge=1,
        description="Block all message types from this zone member.",
    )
    blocked_message_type: MessageFeatureType | None = Field(
        default=None,
        description="Block this message type from all senders in the zone.",
    )

    @model_validator(mode="after")
    def validate_any_selector(self):
        if self.blocked_owner_id is None and self.blocked_message_type is None:
            raise ValueError("Either blocked_owner_id or blocked_message_type is required")
        if self.blocked_owner_id is not None and self.blocked_message_type is not None:
            raise ValueError("Send only blocked_owner_id or blocked_message_type, not both")
        return self


class BlockRuleResponse(BaseModel):
    id: int
    owner_id: int
    blocked_owner_id: int | None
    blocked_message_type: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AccessScheduleCreate(BaseModel):
    zone_id: str = Field(..., min_length=1, max_length=100, description="Target zone id (matches QR / owner zone).")
    event_id: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "Optional event id matched on guest arrival with **POST /api/access/permission**. "
            "Uses the same canonical rules as guest passes (EVT-numeric vs bare digits, case-insensitive otherwise)."
        ),
    )
    guest_id: str | None = Field(default=None, max_length=100, description="Optional pre-provisioned guest id.")
    guest_name: str | None = Field(default=None, max_length=255, description="Expected guest display name.")
    starts_at: datetime | None = Field(default=None, description="Window start (UTC); null means open-ended past.")
    ends_at: datetime | None = Field(default=None, description="Window end (UTC); null means open-ended future.")
    notify_member_assist: bool = Field(
        default=False,
        description="When true, zone administrators also receive assist notifications for this schedule.",
    )


class AccessScheduleResponse(BaseModel):
    id: int
    zone_id: str
    event_id: str | None
    guest_id: str | None
    guest_name: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    notify_member_assist: bool
    active: bool
    created_by_owner_id: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PermissionDecisionResponse(BaseModel):
    decision: str = Field(description="EXPECTED_GUEST or NOT_EXPECTED_GUEST.")
    schedule_match: bool = Field(description="True when an active schedule matched the incoming payload.")
    sender_message: dict = Field(description="Short codes/text for the requesting client.")
    member_message: dict = Field(description="Short codes/text for notified members.")
    delivered_owner_ids: list[int] = Field(description="Owner ids receiving downstream WebSocket PERMISSION_MESSAGE.")


class PropagationMessageResponse(BaseModel):
    id: str | None = None
    sender_id: int | None = Field(
        default=None,
        description="Owner id that sent the geo message (for inbox + WebSocket clients).",
    )
    zone_id: str | None = Field(
        default=None,
        description="Primary zone id on the persisted ZoneMessageEvent.",
    )
    type: str | None = None
    category: str | None = None
    scope: str | None = None
    zone_ids: list[str] = Field(default_factory=list)
    delivered_owner_ids: list[int] = Field(default_factory=list)
    blocked_owner_ids: list[int] = Field(default_factory=list)
    created_at: str | None = None
    text: str | None = None
    skipped: bool = False
    reason: str | None = None
    fanout: dict | None = None
    metadata: dict | None = None
    push_sent: int | None = None
    push_failed: int | None = None
