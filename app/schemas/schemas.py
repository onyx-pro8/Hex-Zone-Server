"""Pydantic schemas for request/response validation."""
from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field, model_validator, computed_field
from typing import Optional, List
from datetime import datetime
from enum import Enum


class AccountTypeEnum(str, Enum):
    """Account type enum."""
    PRIVATE = "private"
    PRIVATE_PLUS = "private_plus"
    EXCLUSIVE = "exclusive"
    ENHANCED = "enhanced"
    ENHANCED_PLUS = "enhanced_plus"


class OwnerRoleEnum(str, Enum):
    """Owner role enum."""
    ADMINISTRATOR = "administrator"
    USER = "user"


class ZoneTypeEnum(str, Enum):
    """Zone type enum."""
    WARN = "warn"
    ALERT = "alert"
    GEOFENCE = "geofence"
    EMERGENCY = "emergency"
    RESTRICTED = "restricted"
    CUSTOM_1 = "custom_1"
    CUSTOM_2 = "custom_2"


# ==================== OWNER SCHEMAS ====================

class OwnerBase(BaseModel):
    """Base owner schema."""
    email: EmailStr
    zone_id: str = Field(..., min_length=1, max_length=100)
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    account_type: AccountTypeEnum = AccountTypeEnum.PRIVATE
    role: OwnerRoleEnum = OwnerRoleEnum.ADMINISTRATOR
    account_owner_id: Optional[int] = Field(None, ge=1)
    address: str = Field(..., min_length=1, max_length=255)
    phone: Optional[str] = Field(None, max_length=20)


class OwnerCreate(BaseModel):
    """Owner creation schema."""
    email: EmailStr = Field(..., description="Username/email used to login")
    zone_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Zone ID entered, generated, or scanned from QR in setup wizard",
    )
    first_name: Optional[str] = Field(None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(None, min_length=1, max_length=100)
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    account_type: AccountTypeEnum = AccountTypeEnum.PRIVATE
    role: OwnerRoleEnum = OwnerRoleEnum.ADMINISTRATOR
    account_owner_id: Optional[int] = Field(
        None,
        ge=1,
        description="Required for user role when joining an existing administrator account.",
    )
    address: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Combined contact address (street/city/province/country)",
    )
    phone: Optional[str] = Field(None, max_length=20, description="Telephone")
    password: str = Field(..., min_length=8, description="Account password")
    registration_code: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Required for administrator self-registration: echo the code from "
            "GET /utils/registration-code (preferred) or GET /owners/registration-code, "
            "or tier code FREE (stateless; always accepted for admin signup). "
            "Not required for user role joining an existing account."
        ),
    )

    @model_validator(mode="after")
    def map_name_to_split_fields(self):
        """Accept either name or first_name/last_name during registration."""
        if self.first_name and self.last_name:
            return self

        if self.name:
            parts = self.name.strip().split()
            if parts:
                self.first_name = self.first_name or parts[0]
                self.last_name = self.last_name or (" ".join(parts[1:]) if len(parts) > 1 else "User")

        if not self.first_name:
            raise ValueError("first_name is required when name is not provided")
        if not self.last_name:
            raise ValueError("last_name is required when name is not provided")

        return self

    model_config = {
        "json_schema_extra": {
            "example": {
                "email": "admin@example.com",
                "zone_id": "ZONE-7A29",
                "first_name": "Avery",
                "last_name": "Stone",
                "account_type": "private",
                "role": "administrator",
                "address": "101 Main St, Denver, CO, USA",
                "phone": "+1-303-555-0114",
                "password": "strong-password-123",
                "registration_code": "FREE",
            }
        }
    }


class OwnerUpdate(BaseModel):
    """Owner update schema."""
    zone_id: Optional[str] = Field(None, min_length=1, max_length=100)
    first_name: Optional[str] = Field(None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(None, min_length=1, max_length=100)
    active: Optional[bool] = None


class OwnerResponse(OwnerBase):
    """Owner response schema."""
    id: int
    api_key: str
    active: bool
    expired: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class OwnerListResponse(BaseModel):
    """Safe owner list schema for receiver discovery."""
    id: int
    first_name: str
    last_name: str
    email: EmailStr
    zone_id: str
    active: bool

    class Config:
        from_attributes = True


class OwnerDetailResponse(OwnerResponse):
    """Detailed owner response with relationships."""
    devices: List["DeviceResponse"] = []
    zones: List["ZoneResponse"] = []

    class Config:
        from_attributes = True


# ==================== DEVICE SCHEMAS ====================

class DeviceBase(BaseModel):
    """Base device schema."""
    hid: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=255)
    address: Optional[str] = None
    propagate_enabled: bool = True
    propagate_radius_km: float = Field(default=1.0, ge=0.1, le=50.0)
    enable_notification: bool = True
    alert_threshold_meters: float = Field(default=100.0, ge=1.0, le=1_000_000.0)
    update_interval_seconds: int = Field(default=60, ge=1, le=86400)


class DeviceLocationUpdate(BaseModel):
    """Device location update schema."""
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    address: Optional[str] = None


class DeviceCreate(DeviceBase):
    """Device creation schema."""
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    active: Optional[bool] = None
    status: Optional[bool] = None
    is_online: Optional[bool] = None

    @model_validator(mode="after")
    def map_status_to_active(self):
        """Allow clients to send either status or active."""
        if self.status is not None:
            if self.active is not None and self.active != self.status:
                raise ValueError("active and status must match when both are provided")
            self.active = self.status
        return self


class DeviceUpdate(BaseModel):
    """Device update schema."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    address: Optional[str] = None
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    propagate_enabled: Optional[bool] = None
    propagate_radius_km: Optional[float] = Field(None, ge=0.1, le=50.0)
    active: Optional[bool] = None
    status: Optional[bool] = None
    is_online: Optional[bool] = None
    enable_notification: Optional[bool] = None
    alert_threshold_meters: Optional[float] = Field(None, ge=1.0, le=1_000_000.0)
    update_interval_seconds: Optional[int] = Field(None, ge=1, le=86400)

    @model_validator(mode="after")
    def map_status_to_active(self):
        """Allow clients to send either status or active."""
        if self.status is not None:
            if self.active is not None and self.active != self.status:
                raise ValueError("active and status must match when both are provided")
            self.active = self.status
        return self


class DeviceOwnerBrief(BaseModel):
    """Minimal owner info returned with device payloads."""
    id: int
    email: EmailStr
    first_name: str
    last_name: str
    role: OwnerRoleEnum
    account_type: AccountTypeEnum
    active: bool

    class Config:
        from_attributes = True


class DeviceResponse(BaseModel):
    """Device response schema."""
    id: int
    hid: str
    device_id: str
    name: str
    latitude: Optional[float]
    longitude: Optional[float]
    address: Optional[str]
    h3_cell_id: Optional[str]
    owner_id: int
    owner: Optional[DeviceOwnerBrief] = None
    propagate_enabled: bool
    propagate_radius_km: float
    active: bool
    is_online: bool
    last_seen: Optional[datetime]
    enable_notification: bool
    alert_threshold_meters: float
    update_interval_seconds: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @computed_field
    @property
    def status(self) -> bool:
        """Backwards-compatible status alias for active."""
        return self.active


# ==================== ZONE SCHEMAS ====================

class ZoneBase(BaseModel):
    """Base zone schema."""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    zone_type: ZoneTypeEnum
    parameters: Optional[dict] = None


class ZoneCreate(ZoneBase):
    """Zone creation schema."""
    zone_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Main zone or optional Zone #2/#3 shared identifier",
    )
    h3_cells: List[str] = Field(
        default_factory=list,
        description="Hex cell IDs for H3/grid-based zone configurations",
    )
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    h3_resolution: Optional[int] = Field(None, ge=0, le=15)
    geo_fence_polygon: Optional[dict] = Field(
        None,
        description="GeoJSON polygon for geofence/object zoning",
    )


class ZoneUpdate(BaseModel):
    """Zone update schema."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    zone_type: Optional[ZoneTypeEnum] = None
    parameters: Optional[dict] = None
    h3_cells: Optional[List[str]] = None
    geo_fence_polygon: Optional[dict] = None
    active: Optional[bool] = None


class ZoneResponse(BaseModel):
    """Zone response schema."""
    id: int
    zone_id: str
    owner_id: int
    creator_id: int
    zone_type: ZoneTypeEnum
    name: str
    description: Optional[str]
    h3_cells: List[str]
    geo_fence_polygon: Optional[dict] = None
    parameters: Optional[dict]
    active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ==================== QR REGISTRATION SCHEMAS ====================

class QRRegistrationCreate(BaseModel):
    """QR registration creation schema."""
    expires_in_hours: int = Field(default=24, ge=1, le=720)


class QRRegistrationResponse(BaseModel):
    """QR registration response schema."""
    id: int
    token: str
    owner_id: int
    used: bool
    expires_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True


class QRRegistrationUse(BaseModel):
    """QR registration use schema (for joining account)."""
    token: str = Field(..., min_length=1)
    email: EmailStr
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=8)
    address: str = Field(..., min_length=1, max_length=255)
    phone: Optional[str] = Field(None, max_length=20)


# ==================== AUTH SCHEMAS ====================

class LoginRequest(BaseModel):
    """Login request schema."""
    email: EmailStr = Field(..., description="Registered username/email")
    password: str = Field(..., description="Registered account password")

    model_config = {
        "json_schema_extra": {
            "example": {
                "email": "admin@example.com",
                "password": "strong-password-123",
            }
        }
    }


class TokenResponse(BaseModel):
    """Token response schema."""
    access_token: str
    token_type: str
    owner_id: int


# ==================== UTILITY SCHEMAS ====================

class H3ConversionRequest(BaseModel):
    """H3 conversion request schema."""
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    resolution: Optional[int] = Field(None, ge=0, le=15)


class H3ConversionResponse(BaseModel):
    """H3 conversion response schema."""
    latitude: float
    longitude: float
    h3_cell_id: str
    resolution: int


# ==================== ZONE MESSAGE SCHEMAS ====================


class MessageVisibilityEnum(str, Enum):
    """Message visibility for zone chat."""

    PUBLIC = "public"
    PRIVATE = "private"


class ZoneMessageCreate(BaseModel):
    """Create a zone **member** message.

    **Default:** persists a **`Message`** row (member ↔ member) when **`guest_id`** is omitted.

    **Guest thread:** set **`guest_id`** and **`zone_id`** (or **`zoneId`**) to append a **`ZoneMessageEvent`**
    for **PERMISSION** or **CHAT** (same store as **`GET /api/guest/messages`**). Omit **`receiver_id`**.
    """

    message: str = Field(..., min_length=1, max_length=16_384)
    type: Optional[str] = Field(
        default=None,
        description="Canonical **`Message`** / event type (e.g. **CHAT**, **PERMISSION**, **SERVICE**). Required unless **visibility** alone is sent (legacy).",
    )
    visibility: Optional[MessageVisibilityEnum] = Field(
        default=None,
        description="Deprecated legacy field. If sent without type, maps private->PRIVATE, public->SERVICE.",
    )
    receiver_id: Optional[int] = Field(
        None,
        ge=1,
        description="Required when visibility is private; omitted for public",
    )
    guest_id: Optional[str] = Field(
        default=None,
        max_length=36,
        description="When set with **zone_id**, creates a **PERMISSION**/**CHAT** **ZoneMessageEvent** for that guest (not **messages** table).",
    )
    zone_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("zone_id", "zoneId"),
        description="Target zone for member→guest messaging; required when **guest_id** is set.",
    )

    @model_validator(mode="after")
    def validate_type_or_visibility(self):
        from app.domain.message_types import LEGACY_VISIBILITY_TO_TYPE

        if self.type:
            return self
        if self.visibility:
            self.type = LEGACY_VISIBILITY_TO_TYPE[self.visibility.value].value
            return self
        raise ValueError("type is required (or send legacy visibility for temporary compatibility)")

    @model_validator(mode="after")
    def guest_thread_fields(self):
        gid = (self.guest_id or "").strip()
        zid = (self.zone_id or "").strip()
        if gid and not zid:
            raise ValueError("zone_id is required when guest_id is set")
        if gid and self.receiver_id is not None:
            raise ValueError("receiver_id must be omitted when guest_id is set")
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "message": "Hello team",
                    "type": "CHAT",
                    "visibility": "private",
                    "receiver_id": 2,
                },
                {
                    "message": "Please proceed to reception.",
                    "type": "PERMISSION",
                    "visibility": "private",
                    "zone_id": "ZN-1XOJPP",
                    "guest_id": "019b2c3d-0000-7000-8000-000000000001",
                },
            ]
        }
    )


class ZoneMessageResponse(BaseModel):
    """Created **`Message`** (numeric **`id`**) or **`ZoneMessageEvent`** (string UUID **`id`**) for guest threads."""

    id: int | str = Field(
        ...,
        description="**`messages.id`** (integer) for normal posts, or **`zone_message_events.id`** (UUID string) when **`guest_id`** was used.",
    )
    zone_id: str = Field(..., description="Shared zone id string (**not** the internal **`zones.id`** PK).")
    sender_id: int = Field(..., description="**`owners.id`** of the sender (always set for member posts).")
    receiver_id: Optional[int] = Field(
        default=None,
        description="Recipient **`owners.id`** for private member messages; **null** for guest-thread events.",
    )
    type: str = Field(description="Canonical message type string (e.g. **CHAT**, **PERMISSION**).")
    category: str = Field(description="Derived **Access** / **Alarm** / **Alert** grouping.")
    scope: str = Field(description="**public** or **private** scope for this type.")
    visibility: MessageVisibilityEnum = Field(description="Legacy visibility; aligns with **scope** for new clients.")
    message: str = Field(description="Body text stored for the message or zone event.")
    created_at: datetime = Field(description="Server **UTC** creation time.")

    model_config = ConfigDict(from_attributes=True)


# Update forward references
OwnerDetailResponse.model_rebuild()
