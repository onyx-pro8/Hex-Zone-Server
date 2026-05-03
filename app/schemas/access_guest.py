"""OpenAPI schemas for public QR guest access (`/api/access/*`)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_GUEST_QR_TOKEN_TTL_HOURS = 24 * 365


class GuestArrivalLocation(BaseModel):
    """Optional GPS hint from the guest device."""

    lat: float = Field(..., ge=-90, le=90, description="Latitude (WGS84).")
    lng: float = Field(..., ge=-180, le=180, description="Longitude (WGS84).")


class GuestArrivalRequest(BaseModel):
    """Payload when a guest scans the zone QR (no JWT). Matches zone + schedule rules server-side."""

    zone_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Zone id from static QR (`?zid=`). Omit when **guest_qr_token** is sent.",
    )
    guest_qr_token: str | None = Field(
        default=None,
        min_length=8,
        max_length=96,
        description=(
            "Opaque secret from SPA **`gt`** ( **`/access?gt=…`**, preferably **`…&zid=…`** from server-mint URLs). "
            "Resolves **zone_id** server-side plus optional token-bound **event_id**."
        ),
    )
    guest_name: str = Field(..., min_length=1, max_length=255, description="Name entered by guest.")
    event_id: str | None = Field(
        default=None,
        max_length=100,
        description="Optional; matched against scheduled visits together with guest_name.",
    )
    device_id: str | None = Field(default=None, max_length=255, description="Optional client device fingerprint.")
    location: GuestArrivalLocation | None = Field(default=None, description="Optional coordinates.")

    @model_validator(mode="after")
    def require_zone_or_guest_token(self):
        zid = (self.zone_id or "").strip()
        gt = (self.guest_qr_token or "").strip()
        if not zid and not gt:
            raise ValueError("Provide zone_id (static QR) and/or guest_qr_token (issued QR).")
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "zone_id": "Z123",
                    "guest_name": "John Doe",
                    "event_id": "EVT-optional",
                    "device_id": "ios-abc",
                    "location": {"lat": 40.7128, "lng": -74.006},
                },
                {
                    "guest_qr_token": "AbCdEf1234567890abcdefghijklmnopQRtoken",
                    "guest_name": "Walk-in",
                    "event_id": "EVT-optional",
                },
                {
                    "guest_qr_token": "AbCdEf1234567890abcdefghijklmnopQRtoken",
                    "zone_id": "ZN-ABC",
                    "guest_name": "Walk-in",
                },
            ]
        }
    )


class GuestZoneActionRequest(BaseModel):
    """Body for **`POST /api/access/approve`** and **`POST /api/access/reject`** (explicit zone).

    SPA routes **`POST /message-feature/access/guest-requests/{guest_id}/approve|reject`** do not use this model:
    **`zone_id`** is derived from **`guest_access_sessions`** for the path **`guest_id`**."""

    guest_id: str = Field(..., min_length=1, max_length=36, description="Returned by **POST /api/access/permission**.")
    zone_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Must match stored **guest_access_sessions.zone_id** for this **guest_id**.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"guest_id": "550e8400-e29b-41d4-a716-446655440000", "zone_id": "Z123"}]
        }
    )


class GuestAccessHttpError(BaseModel):
    """Envelope returned by the global HTTP exception handler for structured errors."""

    status: Literal["error"] = Field(default="error", description="Always `error` for API failures.")
    message: str = Field(description="Human-readable explanation.")
    error_code: str = Field(description="Stable machine-readable code (e.g. INVALID_ZONE, FORBIDDEN).")
    error: dict[str, str] | None = Field(
        default=None,
        description="Optional nested message (handler mirrors **message** here when present).",
    )


class GuestScanResponse(BaseModel):
    """Immediate response after POST /api/access/permission."""

    status: Literal["EXPECTED", "UNEXPECTED"] = Field(
        description="EXPECTED: matched active schedule in window. UNEXPECTED: no matching schedule."
    )
    message: str = Field(description="Guest-facing instruction text.")
    guest_id: str = Field(description="Opaque id for polling GET /api/access/session/{guest_id}.")
    zone_id: str = Field(
        description=(
            "Arrival zone: pass as **`zone_id`** on `GET /api/access/session/{guest_id}` when the invite URL "
            "had no **`zid`**; **`zone_id`** may also be omitted on that GET (server resolves **`guest_id`** alone)."
        ),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "EXPECTED",
                    "message": "You are expected. Please proceed.",
                    "guest_id": "550e8400-e29b-41d4-a716-446655440000",
                    "zone_id": "ZN-ABC",
                },
                {
                    "status": "UNEXPECTED",
                    "message": "You are not scheduled. Please wait for approval.",
                    "guest_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                    "zone_id": "ZN-ABC",
                },
            ]
        }
    )


class GuestAdminDecisionResponse(BaseModel):
    """Successful approve/reject body from **POST /api/access/approve** / **reject** or
    **POST /message-feature/access/guest-requests/{guest_id}/approve** / **reject**."""

    status: Literal["APPROVED", "REJECTED"] = Field(description="Resolution broadcast to polling clients.")
    message: str = Field(description="Short confirmation shown to admin caller / echoed for logs.")
    guest_id: str = Field(description="Same guest id issued at arrival.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"status": "APPROVED", "message": "Guest access approved.", "guest_id": "550e8400-e29b-41d4-a716-446655440000"}
            ]
        }
    )


class GuestAccessSessionListItem(BaseModel):
    """One QR arrival session returned to authenticated zone members.

    **`guest_id`** feeds **`POST …/guest-requests/{guest_id}/approve|reject`** (no **`zone_id`** in path required)."""

    id: int
    guest_id: str = Field(description="Use on **approve**/**reject** dashboard routes (opaque session id).")
    zone_id: str
    qr_token_id: int | None = Field(default=None, description="Set when arrival used a stored guest QR token.")
    guest_name: str
    event_id: str | None = None
    device_id: str | None = None
    kind: str = Field(description="expected | unexpected")
    resolution: str | None = Field(description="pending | approved | rejected; null for expected arrivals.")
    schedule_id: int | None = None
    admin_owner_id: int | None = Field(description="Anchor admin for unexpected chat thread when set.")
    latitude: float | None = None
    longitude: float | None = None
    created_at: datetime
    guest_status: Literal["EXPECTED", "UNEXPECTED", "APPROVED", "REJECTED"] = Field(
        description="Derived guest-facing status (matches GET /api/access/session/{guest_id})."
    )


class GuestAccessQrLinkResponse(BaseModel):
    """Administrator fetch of the stable guest deep link (same string encoded by GET /api/access/qr.png)."""

    url: str | None = Field(
        default=None,
        description=(
            "Absolute URL when **GUEST_ACCESS_APP_BASE_URL** (or legacy **PUBLIC_WEB_APP_URL**) is set; "
            "otherwise null — use **path_with_query** with your known web origin."
        ),
    )
    zone_id: str = Field(description="Echo of the requested zone id (matches query/body elsewhere).")
    path_with_query: str = Field(
        ...,
        description="`/access?zid=...` with optional `eid`; no PII. Encode this in QR if **url** is null.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "url": "https://app.example.com/access?zid=ZN-1XOJPP&eid=EVT1",
                    "zone_id": "ZN-1XOJPP",
                    "path_with_query": "/access?zid=ZN-1XOJPP&eid=EVT1",
                },
                {"url": None, "zone_id": "ZN-1XOJPP", "path_with_query": "/access?zid=ZN-1XOJPP"},
            ]
        }
    )


class GuestQrTokenCreate(BaseModel):
    """Mint a time-bound guest door token; SPA **`/access?gt=…&zid=…`** (optional **`eid`**). JWT administrator required."""

    zone_id: str = Field(..., min_length=1, max_length=100)
    expires_in_hours: float | None = Field(
        default=None,
        ge=1,
        le=float(MAX_GUEST_QR_TOKEN_TTL_HOURS),
        description=f"If set (without expires_at), TTL from now. Max {MAX_GUEST_QR_TOKEN_TTL_HOURS}h.",
    )
    expires_at: datetime | None = Field(default=None, description="Absolute expiry (UTC naive or ISO).")
    event_id: str | None = Field(default=None, max_length=100, description="Bind arrivals to this event id.")
    label: str | None = Field(default=None, max_length=255, description="Dashboard label.")
    max_uses: int | None = Field(default=None, ge=1, description="Cap successful arrivals; omit for unlimited.")

    @model_validator(mode="after")
    def expires_exclusive(self):
        if self.expires_at is not None and self.expires_in_hours is not None:
            raise ValueError("Provide either expires_at or expires_in_hours, not both.")
        return self


class GuestQrTokenListItem(BaseModel):
    """Stored guest QR metadata (never includes full secret after creation)."""

    id: int
    zone_id: str
    event_id: str | None = None
    label: str | None = None
    expires_at: datetime
    revoked_at: datetime | None = None
    max_uses: int | None = None
    use_count: int = 0
    created_at: datetime
    last_used_at: datetime | None = None
    created_by_owner_id: int
    token_suffix: str = Field(description="Last characters of token for display.")


class GuestQrTokenCreatedResponse(GuestQrTokenListItem):
    """Returned once when minting; includes secret **token** for QR encoding."""

    token: str = Field(description="Opaque **gt** value; URLs from this API also include **zid** (and **eid** when set). Not stored in list APIs.")
    url: str | None = Field(description="Absolute URL when **GUEST_ACCESS_APP_BASE_URL** (or legacy **PUBLIC_WEB_APP_URL**) is set.")
    path_with_query: str = Field(
        description="SPA path **`/access?gt=…&zid=…`** ( **`eid`** appended when bound on this token)."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": 1,
                    "zone_id": "ZN-DEMO",
                    "event_id": "EVT01",
                    "label": None,
                    "expires_at": "2026-12-31T23:59:59",
                    "revoked_at": None,
                    "max_uses": None,
                    "use_count": 0,
                    "created_at": "2026-01-01T12:00:00",
                    "last_used_at": None,
                    "created_by_owner_id": 42,
                    "token_suffix": "Qr8x",
                    "token": "__secret_only_at_create__",
                    "url": "https://app.example.com/access?gt=__secret_only_at_create__&zid=ZN-DEMO&eid=EVT01",
                    "path_with_query": "/access?gt=__secret_only_at_create__&zid=ZN-DEMO&eid=EVT01",
                }
            ]
        }
    )


class GuestQrTokenLinkBundle(BaseModel):
    """Resolved URL for an existing stored token (admin); secret never appears in list APIs."""

    id: int = Field(description="Row id (**guest_access_qr_tokens**).")
    url: str | None = Field(default=None, description="Absolute SPA URL (**gt + zid**), if web base env is configured.")
    path_with_query: str = Field(
        description=(
            "`/access?gt=…&zid=…` (optional **`eid`**); same rules as **`POST /api/access/qr-tokens`**."
        )
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": 7,
                    "url": "https://app.example.com/access?gt=opaque&zid=ZN-1&eid=E1",
                    "path_with_query": "/access?gt=opaque&zid=ZN-1&eid=E1",
                }
            ]
        }
    )


class GuestSessionPollResponse(BaseModel):
    """Poll shape after permission; callers may use **`GET …/session/{guest_id}?zone_id=`** or omit **`zone_id`**."""

    guest_id: str = Field(description="Same **guest_id** returned by **`POST /api/access/permission`**.")
    zone_id: str = Field(description="Session zone (echoed for display; poll may filter by **`zone_id`** query or omit it).")
    status: Literal["EXPECTED", "UNEXPECTED", "APPROVED", "REJECTED"] = Field(
        description="EXPECTED: scheduled guest; UNEXPECTED: still pending admin; APPROVED/REJECTED: resolved."
    )
    message: str
    exchange_code: str | None = Field(
        default=None,
        description="One-time code for **`POST /api/access/guest-session`** when **status** is **APPROVED** only.",
    )
    exchange_expires_at: str | None = Field(
        default=None,
        description="ISO-8601 UTC instant when **exchange_code** expires (only with **exchange_code**).",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "guest_id": "550e8400-e29b-41d4-a716-446655440000",
                    "zone_id": "Z123",
                    "status": "UNEXPECTED",
                    "message": "You are not scheduled. Please wait for approval.",
                },
                {
                    "guest_id": "550e8400-e29b-41d4-a716-446655440000",
                    "zone_id": "Z123",
                    "status": "APPROVED",
                    "message": "Your visit has been approved. Welcome.",
                    "exchange_code": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                    "exchange_expires_at": "2026-05-03T15:00:00Z",
                },
            ]
        }
    )


class GuestSessionExchangeRequest(BaseModel):
    """Body for **`POST /api/access/guest-session`** (no Bearer)."""

    guest_id: str = Field(..., min_length=1, max_length=36, description="Same opaque id as **GET /api/access/session/{guest_id}**.")
    zone_id: str = Field(..., min_length=1, max_length=100, description="Must equal **guest_access_sessions.zone_id** for this guest.")
    exchange_code: str = Field(
        ...,
        min_length=1,
        max_length=36,
        description="UUID returned on the poll response when **status** is **APPROVED** (single use).",
    )
    device_id: str | None = Field(
        default=None,
        max_length=255,
        description="Optional; if the arrival stored **device_id** and this differs, **403** `device_mismatch`.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "guest_id": "550e8400-e29b-41d4-a716-446655440000",
                    "zone_id": "ZN-DEMO",
                    "exchange_code": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
                    "device_id": "ios-client-abc",
                }
            ]
        }
    )
