# Guest Access API Contract

This contract is locked for frontend compatibility and must be kept 1:1.

OpenAPI remains available at `/docs` and `/redoc`.

## Workflow Rules

- One `zone_id` has one active reusable primary guest token.
- Primary token is persistent and does not auto-expire.
- Access permission lifetime is controlled by scheduling state (`end_time` / duration semantics), not QR-token expiry.
- `PERMISSION` messages/events are server-generated only for guest workflow transitions.
- Guest manual messaging after login is `CHAT` only.

## Endpoint Contracts

### A) Guest QR per zone

#### 1) Get-or-create primary token

`GET /api/access/qr-tokens/primary?zone_id={zone_id}`

Response `200`:

```json
{
  "status": "success",
  "data": {
    "id": 12,
    "zone_id": "ZN-ABC",
    "token_suffix": "a1b2c3",
    "url": "https://app.example.com/access?gt=...&zid=ZN-ABC",
    "path_with_query": "/access?gt=...&zid=ZN-ABC",
    "revoked_at": null,
    "created_at": "2026-05-06T14:00:00",
    "updated_at": "2026-05-06T14:00:00"
  }
}
```

Behavior:

- Existing active primary token is returned.
- Missing token is created and returned.
- `expires_at` is not used for this primary flow.

#### 2) Rotate primary token

`POST /api/access/qr-tokens/primary/rotate`

Request:

```json
{
  "zone_id": "ZN-ABC",
  "reason": "optional reason"
}
```

Response `200`: same shape as get-or-create.

Behavior:

- Existing active primary token(s) are revoked.
- New active primary token is issued.
- Audit trail is preserved.

#### 3) Backward compatibility endpoints

- `POST /api/access/qr-tokens`
- `GET /api/access/qr-tokens`

For primary mode (`is_primary=true`):

- Primary token remains non-expiring (`expires_at = null`).
- Sending `expires_at` or `expires_in_hours` with primary mode is rejected with clear validation:
  - `error_code: PRIMARY_TOKEN_EXPIRY_NOT_ALLOWED`

### B) Guest submission and polling

#### 4) Submit guest access request

`POST /api/access/permission` (anonymous)

Request:

```json
{
  "guest_qr_token": "string",
  "zone_id": "string",
  "guest_name": "string",
  "event_id": "optional string",
  "device_id": "optional string",
  "location": { "lat": 10.1, "lng": 20.2 },
  "sig": "optional string"
}
```

Success response `200`:

```json
{
  "status": "success",
  "data": {
    "status": "UNEXPECTED",
    "message": "You are not scheduled. Please wait for approval.",
    "guest_id": "550e8400-e29b-41d4-a716-446655440000",
    "zone_id": "ZN-ABC"
  }
}
```

Error response shape:

```json
{
  "status": "error",
  "error_code": "STRING_CODE",
  "message": "human-readable message"
}
```

#### 5) Poll approval/session state

`GET /api/access/session/{guest_id}?zone_id={zone_id}`

Response `200`:

```json
{
  "status": "success",
  "data": {
    "status": "PENDING",
    "message": "You are not scheduled. Please wait for approval."
  }
}
```

Approved example:

```json
{
  "status": "success",
  "data": {
    "status": "APPROVED",
    "message": "Your visit has been approved. Welcome.",
    "exchange_code": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "exchange_expires_at": "2026-05-06T14:30:00Z"
  }
}
```

### C) Admin guest request management

#### 6) List requests by zone

`GET /api/access/guest-requests?zone_id={zone_id}`

Response `200`:

```json
{
  "status": "success",
  "data": [
    {
      "id": "123",
      "guest_id": "550e8400-e29b-41d4-a716-446655440000",
      "zone_id": "ZN-ABC",
      "guest_name": "Guest Name",
      "status": "PENDING",
      "expectation": "unexpected",
      "created_at": "2026-05-06T14:00:00",
      "hid": "ios-device-id"
    }
  ]
}
```

#### 7) Approve request

`POST /message-feature/access/guest-requests/{requestId}/approve?zone_id={zone_id}`

#### 8) Reject request

`POST /message-feature/access/guest-requests/{requestId}/reject?zone_id={zone_id}`

Response `200`:

```json
{
  "status": "success",
  "data": {
    "id": "123",
    "status": "APPROVED",
    "zone_id": "ZN-ABC",
    "updated_at": "2026-05-06T14:05:00"
  }
}
```

## Automatic PERMISSION message policy

`PERMISSION` is server-generated only for:

1. Guest request submitted
2. Admin approved
3. Admin rejected

Manual compose for `PERMISSION` is rejected:

- `error_code: PERMISSION_MANUAL_DISABLED`

`PERMISSION` remains visible in thread/history; `CHAT` stays user-generated.

### Member/admin: read the same thread as the guest

Guests load history with `GET /api/guest/messages`. Members who administer the zone should use:

`GET /api/access/guest-messages?zone_id={zone_id}&guest_id={guest_id}`

Response `200`:

```json
{
  "status": "success",
  "data": {
    "items": [
      {
        "id": "uuid",
        "zone_id": "ZN-ABC",
        "sender_id": null,
        "receiver_id": null,
        "type": "PERMISSION",
        "category": "Access",
        "scope": "private",
        "visibility": "private",
        "message": "You are not scheduled. Please wait for approval.",
        "created_at": "2026-05-06T14:00:00"
      }
    ]
  }
}
```

Optional query `with_owner_id={owners.id}` narrows to messages involving that peer (aligned with the guest’s `with_owner_id` filter).

Legacy-compatible listing (no separate frontend routes required):

- `GET /messages?owner_id={member_owner_id}&guest_id={guest_id}&zone_id={zone_id}`
- CamelCase aliases: **`guestId`**, **`zoneId`**, **`requestId`** (numeric **`guest_access_sessions.id`** from **`GET /api/access/guest-requests`**, matching **`data[].id`**)

These return the same **PERMISSION** + **CHAT** **items** as a bare JSON array (`ZoneMessageResponse` shape).

## Guest Role Boundaries

Guest can:

- Read own session/profile data
- Read allowed zone dashboard payload
- Read allowed member/peer visibility data
- Read guest message thread
- Post `CHAT` in allowed guest thread

Guest cannot:

- Create/update/delete zones
- Create admin/member resources
- Send non-`CHAT` message types

## Stable Error Codes

- `INVALID_GUEST_TOKEN`
- `TOKEN_ZONE_MISMATCH`
- `PERMISSION_MANUAL_DISABLED`
- `GUEST_MESSAGE_TYPE_NOT_ALLOWED`
- `GUEST_NOT_AUTHORIZED_FOR_ZONE`
- `ACCESS_REQUEST_NOT_FOUND`
