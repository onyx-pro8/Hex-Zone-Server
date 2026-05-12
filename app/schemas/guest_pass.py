"""Pydantic schemas for the guest pass feature (/api/access/guest-passes)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GuestPassCreateRequest(BaseModel):
    """Body for **`POST /api/access/guest-passes`** (member JWT)."""

    zone_id: str = Field(..., min_length=1, max_length=100, description="Zone this guest pass belongs to.")
    event_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Unique event identifier the guest will present on arrival. Case-insensitive matching.",
    )
    guest_name: str | None = Field(
        default=None,
        max_length=255,
        description="Expected guest name (informational only; not used for matching).",
    )
    notes: str | None = Field(default=None, max_length=1000, description="Reason or description for the pass.")
    expires_at: datetime = Field(..., description="ISO 8601 UTC datetime when this pass becomes invalid.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "zone_id": "ZN-1XOJPP",
                    "event_id": "EVT-2026-0515",
                    "guest_name": "Jane Doe",
                    "notes": "Client meeting, conference room B",
                    "expires_at": "2026-05-15T18:00:00Z",
                },
                {
                    "zone_id": "ZN-1XOJPP",
                    "event_id": "DELIVERY-42",
                    "expires_at": "2026-05-13T12:00:00Z",
                },
            ]
        }
    )


class GuestPassCreatedData(BaseModel):
    """Successful response data from **`POST /api/access/guest-passes`**."""

    id: str = Field(description="UUID primary key of the new guest pass.")
    zone_id: str = Field(description="Zone the pass belongs to.")
    event_id: str = Field(description="Unique event identifier for guest arrival matching.")
    guest_name: str | None = Field(default=None, description="Expected guest name (informational).")
    notes: str | None = Field(default=None, description="Reason or description.")
    status: Literal["PENDING"] = Field(default="PENDING", description="Always PENDING on creation.")
    requested_by: int = Field(description="owner_id of the member who created the pass.")
    expires_at: datetime = Field(description="ISO 8601 UTC expiry.")
    created_at: datetime = Field(description="ISO 8601 UTC creation timestamp.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                    "zone_id": "ZN-1XOJPP",
                    "event_id": "EVT-2026-0515",
                    "guest_name": "Jane Doe",
                    "notes": "Client meeting, conference room B",
                    "status": "PENDING",
                    "requested_by": 123,
                    "expires_at": "2026-05-15T18:00:00",
                    "created_at": "2026-05-12T10:30:00",
                }
            ]
        }
    )


class GuestPassCreatedEnvelope(BaseModel):
    """Success envelope for **`POST /api/access/guest-passes`**."""

    status: Literal["success"] = "success"
    data: GuestPassCreatedData


class GuestPassListItem(BaseModel):
    """One guest pass returned in the list response."""

    id: str = Field(description="UUID primary key.")
    zone_id: str = Field(description="Zone the pass belongs to.")
    event_id: str = Field(description="Event identifier for guest matching.")
    guest_name: str | None = Field(default=None, description="Expected guest name (informational).")
    notes: str | None = Field(default=None, description="Reason or description.")
    status: Literal["PENDING", "ACCEPTED", "REJECTED", "REVOKED"] = Field(
        description="Current lifecycle status."
    )
    requested_by: int = Field(description="owner_id of the member who created the pass.")
    requested_by_name: str = Field(description="Display name of the requester.")
    reviewed_by: int | None = Field(default=None, description="owner_id of the admin who accepted/rejected.")
    used_by_guest_id: str | None = Field(
        default=None,
        description="guest_id of the guest who consumed this pass on arrival. Null until used.",
    )
    expires_at: datetime = Field(description="ISO 8601 UTC expiry.")
    created_at: datetime = Field(description="ISO 8601 UTC creation timestamp.")
    updated_at: datetime = Field(description="ISO 8601 UTC last-updated timestamp.")
    is_expired: bool = Field(description="Computed: true if now > expires_at, regardless of status.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                    "zone_id": "ZN-1XOJPP",
                    "event_id": "EVT-2026-0515",
                    "guest_name": "Jane Doe",
                    "notes": "Client meeting",
                    "status": "ACCEPTED",
                    "requested_by": 123,
                    "requested_by_name": "John Smith",
                    "reviewed_by": 456,
                    "used_by_guest_id": None,
                    "expires_at": "2026-05-15T18:00:00",
                    "created_at": "2026-05-12T10:30:00",
                    "updated_at": "2026-05-12T11:00:00",
                    "is_expired": False,
                }
            ]
        }
    )


class GuestPassListEnvelope(BaseModel):
    """Success envelope for **`GET /api/access/guest-passes`**."""

    status: Literal["success"] = "success"
    data: list[GuestPassListItem] = Field(
        ..., description="Guest passes for the zone, newest first."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"status": "success", "data": []},
            ]
        }
    )


class GuestPassDecisionData(BaseModel):
    """Response data from accept/reject/revoke endpoints."""

    id: str = Field(description="UUID of the guest pass.")
    status: Literal["ACCEPTED", "REJECTED", "REVOKED"] = Field(description="Updated status.")
    reviewed_by: int | None = Field(default=None, description="owner_id of the admin who made the decision.")
    updated_at: datetime = Field(description="ISO 8601 UTC timestamp of the decision.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                    "status": "ACCEPTED",
                    "reviewed_by": 456,
                    "updated_at": "2026-05-12T11:00:00",
                }
            ]
        }
    )


class GuestPassDecisionEnvelope(BaseModel):
    """Success envelope for accept/reject/revoke endpoints."""

    status: Literal["success"] = "success"
    data: GuestPassDecisionData


class GuestPassAdminRequest(BaseModel):
    """Optional body for accept/reject/revoke (empty `{}` is valid)."""

    zone_id: str | None = Field(
        default=None,
        max_length=100,
        description="Optional zone_id for verification; server uses the pass's zone_id regardless.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {},
                {"zone_id": "ZN-1XOJPP"},
            ]
        }
    )
