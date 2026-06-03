"""OpenAPI schemas for approved-guest APIs (`/api/guest/*`) and guest-session exchange."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GuestApiHttpError(BaseModel):
    """Error body produced by the global HTTP exception handler (typical shape).

    For **`/api/guest/*`**, **`401`** often carries **`error_code`** = **`GUEST_ACCESS_INVALIDATED`** when the JWT is
    still cryptographically valid but the backing **`guest_access_sessions`** / guest pass / QR state no longer
    allows access (admin revoke, reject, guest-pass revoke, QR revoke, or pass expiry).
    """

    status: Literal["error"] = "error"
    message: str = Field(description="Human-readable summary.")
    error_code: str = Field(
        description=(
            "Stable code. Guest routes: **`GUEST_ACCESS_INVALIDATED`** (server-side revoke/deny/expired pass), "
            "**`INVALID_GUEST_TOKEN`** (wrong `token_use` / subject), or **`HTTP_401`** when detail was a plain string "
            "(e.g. expired signature from **`verify_token`**). Other codes: `exchange_consumed`, `NOT_FOUND`, "
            "`PERMISSION_MANUAL_DISABLED`, `GUEST_MESSAGE_TYPE_NOT_ALLOWED`, `GUEST_NOT_AUTHORIZED_FOR_ZONE`, "
            "`PEERS_NOT_AVAILABLE`, `VALIDATION`, `FORBIDDEN`."
        )
    )
    error: dict[str, str] = Field(
        default_factory=dict,
        description="Always includes at least `message` mirroring the top-level summary.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "error",
                    "message": "Guest access has been revoked.",
                    "error_code": "GUEST_ACCESS_INVALIDATED",
                    "error": {"message": "Guest access has been revoked."},
                }
            ]
        }
    )


# --- POST /api/access/guest-session ---


class GuestSessionGuestProfile(BaseModel):
    guest_id: str
    display_name: str
    zone_ids: list[str] = Field(description="Zones this token may access (subset of approval).")
    allowed_message_types: list[Literal["CHAT"]] = Field(
        default=["CHAT"],
        description="Types this token may send via **POST /api/guest/messages**.",
    )


class GuestSessionExchangeData(BaseModel):
    access_token: str = Field(
        description=(
            "JWT: use as `Authorization: Bearer …` on **`/api/guest/*`** only. Lifetime = **`expires_in`**. "
            "The server re-validates **`guest_access_sessions`** (and linked guest pass / QR) on **every** guest request; "
            "admin revoke yields **`401`** **`GUEST_ACCESS_INVALIDATED`** even before **`exp`**."
        )
    )
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int = Field(description="Access token lifetime in seconds (see server `GUEST_ACCESS_TOKEN_EXPIRE_MINUTES`).")
    guest: GuestSessionGuestProfile


class GuestSessionExchangeResponse(BaseModel):
    """Success envelope for **POST /api/access/guest-session**."""

    status: Literal["success"] = "success"
    data: GuestSessionExchangeData

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "data": {
                        "access_token": "eyJ…",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "guest": {
                            "guest_id": "550e8400-e29b-41d4-a716-446655440000",
                            "display_name": "Walk-in Pat",
                            "zone_ids": ["ZN-DEMO"],
                            "allowed_message_types": ["CHAT"],
                        },
                    },
                }
            ]
        }
    )


# --- GET /api/guest/me ---


class GuestMeData(BaseModel):
    guest_id: str = Field(description="Opaque id from **`POST /api/access/permission`**; matches **`guest_access_sessions.guest_id`**.")
    display_name: str = Field(description="Name captured at check-in.")
    zone_ids: list[str] = Field(description="Shared zone ids this JWT may access (query **`zone_id`** on nested routes must be listed here).")
    allowed_message_types: list[Literal["CHAT"]] = Field(
        default_factory=lambda: ["CHAT"],
        description="Subset of Access-channel types allowed for **`POST /api/guest/messages`** (CHAT only).",
    )
    expires_at: str = Field(description="ISO-8601 UTC from JWT **`exp`** (guest token lifetime sets **`GET /api/guest/me`** refresh cadence).")


class GuestMeResponse(BaseModel):
    status: Literal["success"] = "success"
    data: GuestMeData

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "data": {
                        "guest_id": "019b2c3d-0000-7000-8000-000000000001",
                        "display_name": "Walk-in Pat",
                        "zone_ids": ["ZN-DEMO"],
                        "allowed_message_types": ["CHAT"],
                        "expires_at": "2026-05-04T14:00:00Z",
                    },
                }
            ]
        }
    )


# --- GET /api/guest/zones/{zone_id}/peers ---


class GuestPeerItem(BaseModel):
    peer_kind: Literal["owner"] = "owner"
    owner_id: int = Field(
        description=(
            "Member account row **`owners.id`** (integer in JSON). Use as **`with_owner_id`** on **GET** "
            "and **`to_owner_id`** on **POST** `/api/guest/messages`."
        ),
    )
    display_name: str
    role: str = Field(description="`administrator` or `user`.")
    can_receive_chat: bool = Field(
        description="False if the member blocked **CHAT** type delivery (guest sends still validated server-side).",
    )


class GuestPeersData(BaseModel):
    zone_id: str
    peers: list[GuestPeerItem]


class GuestPeersResponse(BaseModel):
    """Success wrapper: **`data.peers`** (not a bare array — matches Hex-Zone client normalizers)."""

    status: Literal["success"] = "success"
    data: GuestPeersData

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "data": {
                        "zone_id": "ZN-DEMO",
                        "peers": [
                            {
                                "peer_kind": "owner",
                                "owner_id": 42,
                                "display_name": "Zone Admin",
                                "role": "administrator",
                                "can_receive_chat": True,
                            }
                        ],
                    },
                }
            ]
        }
    )


# --- GET /api/guest/zones/{zone_id}/dashboard ---


class GuestDashboardData(BaseModel):
    zone_id: str
    label: str
    welcome_text: str
    links: list[dict[str, Any]] = Field(default_factory=list, description="Safe v1 links; may be empty.")
    cells: list[str] = Field(
        default_factory=list,
        description="Duplicate of **`map.cells`** (H3 cell ids) for clients that expect a flat list.",
    )
    map: dict[str, Any] | None = Field(
        default=None,
        description=(
            "**`map`** / **`cells`** for guest UI: **`{ center?, zoom, bounds?, geojson?, cells }`**. "
            "Polygon geometry is intentionally omitted unless configured on **`zones.parameters.guest_map`**."
        ),
    )
    zone: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Read-only copy of the zone in the SAME shape members get from **`GET /zones`** "
            "(**`geometry`**, **`config.h3_cells`**, **`geo_fence_polygon`** as GeoJSON). Lets guests "
            "render the exact same map as the owner without any zone-editing capability."
        ),
    )


class GuestDashboardResponse(BaseModel):
    status: Literal["success"] = "success"
    data: GuestDashboardData

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "data": {
                        "zone_id": "ZN-DEMO",
                        "label": "Demo HQ",
                        "welcome_text": "Welcome to the zone guest dashboard.",
                        "links": [],
                        "cells": ["8928308280fffff"],
                        "map": {
                            "center": {"lat": 37.7749, "lng": -122.4194},
                            "zoom": 14,
                            "cells": ["8928308280fffff"],
                            "bounds": {"south": 37.76, "north": 37.79, "east": -122.40, "west": -122.44},
                            "geojson": {"type": "FeatureCollection", "features": []},
                        },
                    },
                }
            ]
        }
    )


# --- GET /api/guest/messages ---


class GuestMessageParticipant(BaseModel):
    kind: Literal["guest", "owner", "zone_broadcast"]
    guest_id: str | None = None
    owner_id: int | None = None


class GuestZoneMessageItem(BaseModel):
    """One zone message row visible to the guest (PERMISSION or CHAT only)."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    zone_id: str
    type: Literal["PERMISSION", "CHAT"]
    created_at: str
    text: str
    from_: GuestMessageParticipant = Field(
        ...,
        alias="from",
        description="Message sender (`guest`, `owner`, or `zone_broadcast` for system-style rows).",
    )
    to: GuestMessageParticipant = Field(description="Recipient (owner, guest, or zone broadcast).")
    raw_payload: dict[str, Any] = Field(default_factory=dict, description="Persisted **body** JSON copy.")


class GuestMessagesListData(BaseModel):
    items: list[GuestZoneMessageItem]
    next_cursor: str | None = Field(default=None, description="Pass as `cursor` for the next page.")


class GuestMessagesListResponse(BaseModel):
    status: Literal["success"] = "success"
    data: GuestMessagesListData

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "data": {
                        "items": [
                            {
                                "id": "019b4c72-9000-7a00-a000-bbbbbbbbbbb1",
                                "zone_id": "ZN-DEMO",
                                "type": "PERMISSION",
                                "created_at": "2026-05-06T14:00:00Z",
                                "text": "Guest access approved for Pat Visitor.",
                                "from": {"kind": "owner", "guest_id": None, "owner_id": 42},
                                "to": {"kind": "zone_broadcast", "guest_id": None, "owner_id": None},
                                "raw_payload": {
                                    "guest_id": "550e8400-e29b-41d4-a716-446655440000",
                                    "guest_request_id": 7,
                                    "guest_name": "Pat Visitor",
                                    "resolution": "APPROVED",
                                },
                            },
                            {
                                "id": "019b4c72-9000-7a00-a000-bbbbbbbbbbb2",
                                "zone_id": "ZN-DEMO",
                                "type": "CHAT",
                                "created_at": "2026-05-06T14:05:00Z",
                                "text": "Hello from guest",
                                "from": {"kind": "guest", "guest_id": "550e8400-e29b-41d4-a716-446655440000", "owner_id": None},
                                "to": {"kind": "owner", "guest_id": None, "owner_id": 42},
                                "raw_payload": {},
                            },
                        ],
                        "next_cursor": None,
                    },
                }
            ]
        }
    )


# --- POST /api/guest/messages ---


class GuestPermissionMsgPayload(BaseModel):
    guest_name: str = Field(..., min_length=1, max_length=255)
    event_id: str | None = Field(default=None, max_length=100)


class GuestMessagePostRequest(BaseModel):
    """Request body for **POST /api/guest/messages**.

    Use **`type`**: **`CHAT`** only for successful delivery. All other values are rejected by policy.
    """

    zone_id: str = Field(..., min_length=1, max_length=100)
    type: str = Field(
        ...,
        min_length=1,
        max_length=32,
        description="Message type: only **CHAT** is allowed; all others are rejected.",
    )
    text: str | None = Field(
        default=None,
        max_length=4000,
        description="Required for **CHAT**.",
    )
    to_owner_id: int = Field(..., ge=1, description="Recipient **owners.id** in this zone.")
    msg: GuestPermissionMsgPayload | None = Field(
        default=None,
        description="Optional extra payload merged into persisted message **body**.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"zone_id": "ZN-1", "type": "CHAT", "text": "Hello", "to_owner_id": 42},
            ]
        }
    )


class GuestMessageCreatedResponse(BaseModel):
    status: Literal["success"] = "success"
    data: GuestZoneMessageItem

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "success",
                    "data": {
                        "id": "019b4c72-9000-7a00-a000-cccccccccc01",
                        "zone_id": "ZN-DEMO",
                        "type": "CHAT",
                        "created_at": "2026-05-06T14:05:30Z",
                        "text": "Hello",
                        "from": {"kind": "guest", "guest_id": "550e8400-e29b-41d4-a716-446655440000", "owner_id": None},
                        "to": {"kind": "owner", "guest_id": None, "owner_id": 42},
                        "raw_payload": {},
                    },
                }
            ]
        }
    )
