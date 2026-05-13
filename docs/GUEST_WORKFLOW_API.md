# Hex-Zone guest workflow — server contract (2026)

Canonical **threading model**: guest access history (PERMISSION + CHAT) lives in **`zone_message_events`**. Each row may set optional **`guest_access_session_id`** → `guest_access_sessions.id` for traceability. **`body_json`** always carries **`guest_id`** (opaque UUID) and, for workflow lines, **`guest_request_id`** (= same session PK), **`guest_name`**, **`zone_id`**.

**Domain metadata** (`metadata_json.domain_event`):

| Event | When |
|--------|------|
| `guest_request_created` | `POST /api/access/permission` → unexpected (pending approval) |
| `guest_expected_arrival` | `POST /api/access/permission` → matched schedule (“expected”) |
| `guest_request_approved` | Admin approve |
| `guest_request_rejected` | Admin reject |

**Admin message inbox**: `GET /messages/?owner_id={self}` merges normal **`messages`** rows with **`PERMISSION`** zone events the caller may administer (`can_manage_zone_guest_requests`). **`GET /messages/?owner_id=&other_owner_id=`** stays member↔member only (no merge).

---

## `POST /api/access/permission` (anonymous)

**Request**

```json
{
  "zone_id": "ZN-DEMO",
  "guest_qr_token": "optional opaque token",
  "guest_name": "Pat Visitor",
  "event_id": null,
  "device_id": null,
  "location": {"lat": 0, "lng": 0},
  "sig": null
}
```

**Success (unexpected)**

```json
{
  "status": "success",
  "data": {
    "status": "UNEXPECTED",
    "message": "You are not scheduled. Please wait for approval.",
    "guest_id": "550e8400-e29b-41d4-a716-446655440000",
    "zone_id": "ZN-DEMO"
  }
}
```

**Side effect**: one **`PERMISSION`** `zone_message_event` with **`text`** like **`Guest access requested for Pat Visitor. Awaiting approval.`** and **`body_json.guest_message`** holding the shorter guest-facing line used in polling.

---

## Member: `POST /api/access/approve` | `reject`

**Approve body**

```json
{ "guest_id": "<opaque>", "zone_id": "ZN-DEMO" }
```

**Success**

```json
{
  "status": "APPROVED",
  "message": "Your visit has been approved. Welcome.",
  "guest_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Side effect**: **`PERMISSION`** row, **`text`** e.g. **`Guest access approved for Pat Visitor.`**, **`body_json.resolution`**: `APPROVED` | `REJECTED`.

Legacy: `POST /message-feature/access/guest-requests/{requestId}/approve|reject?zone_id=` (numeric session id or `guest_id`).

---

## Guest JWT: `POST /api/access/guest-session`

```json
{ "guest_id": "…", "zone_id": "…", "exchange_code": "…", "device_id": null }
```

Returns **`access_token`** (Bearer) with `token_use=guest_access` — **only** for `/api/guest/*`.

---

## Guest JWT invalidation (revocation)

Every **`/api/guest/*`** request re-checks the database. If access was revoked, denied, or the backing guest pass / QR token no longer allows the session, the API returns **`401 Unauthorized`** with body:

```json
{
  "status": "error",
  "message": "<human-readable reason>",
  "error_code": "GUEST_ACCESS_INVALIDATED",
  "error": { "message": "<same as message>" }
}
```

Treat **`401`** + **`GUEST_ACCESS_INVALIDATED`** like an expired or invalid token: clear stored guest JWT and return the guest to the permission / exchange flow.

**Admin actions that invalidate an existing guest Bearer**

| Action | Route / behavior |
|--------|------------------|
| Revoke accepted guest pass (after arrival) | **`POST /api/access/guest-passes/{pass_id}/revoke`** — sets pass **`REVOKED`** and **`guest_access_sessions.access_revoked_at`** for the consumed session when **`used_by_guest_id`** is set. |
| Reject pending unexpected guest | **`POST /api/access/reject`** (or message-feature **`…/reject`**) — **`resolution`** → **`rejected`** (guest never had a valid exchange if still pending; if race, JWT check fails on **`rejected`**). |
| Revoke approved unexpected guest (after JWT issued) | Same **`POST /api/access/reject`** — allowed when **`resolution`** was **`approved`**; sets **`rejected`** and invalidates Bearer. |
| Revoke expected session (schedule / guest-pass auto-expected) | Same **`POST /api/access/reject`** on an **`expected`** session — sets **`access_revoked_at`**. |
| Revoke guest QR token used at arrival | **`POST /api/access/qr-tokens/{id}/revoke`** — if the session row’s **`qr_token_id`** points at a revoked token, guest APIs return **`GUEST_ACCESS_INVALIDATED`**. |

Expired **cryptographic** JWT (`exp` in the past) still yields **`401`** with **`Invalid authentication credentials`** (existing **`verify_token`** behavior). An **ACCEPTED** guest pass past **`expires_at`** also yields **`GUEST_ACCESS_INVALIDATED`** for sessions tied to **`used_by_guest_id`**.

---

## `GET /api/guest/me`

```json
{
  "status": "success",
  "data": {
    "guest_id": "…",
    "display_name": "Pat Visitor",
    "zone_ids": ["ZN-DEMO"],
    "allowed_message_types": ["CHAT"],
    "expires_at": "2026-05-06T14:30:00Z"
  }
}
```

---

## `GET /api/guest/zones/{zone_id}/peers`

Staff only: **`ADMINISTRATOR`** owners with **`owners.zone_id`**, **`zones.owner_id`** for active zones, plus **`resolve_primary_zone_admin_owner`**.

```json
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
        "can_receive_chat": true
      }
    ]
  }
}
```

**Deviation from “flat `data: []`” spec**: retained nested **`data.peers`** to match **`BACKEND_ACCESS_ZONE_FULL_CONTRACT.md`** / client normalizers. Flat array is **not** returned.

---

## `GET /api/guest/messages`

Query: **`zone_id`** (required), **`with_owner_id`** (optional), **`limit`**, optional **`cursor`**.

Returns **`PERMISSION`** + **`CHAT`**, newest first. PERMISSION rows appear in **every** staff-peer thread (`with_owner_id`) for that guest.

```json
{
  "status": "success",
  "data": {
    "items": [
      {
        "id": "uuid",
        "zone_id": "ZN-DEMO",
        "type": "PERMISSION",
        "created_at": "2026-05-06T12:00:00Z",
        "text": "Guest access approved for Pat Visitor.",
        "from": { "kind": "owner", "guest_id": null, "owner_id": 42 },
        "to": { "kind": "zone_broadcast", "guest_id": null, "owner_id": null },
        "raw_payload": {}
      }
    ],
    "next_cursor": null
  }
}
```

**Deviation**: query params also support legacy **`before_id`** / **`cursor`** pagination (documented on the route).

---

## `POST /api/guest/messages`

```json
{
  "zone_id": "ZN-DEMO",
  "type": "CHAT",
  "text": "Hello",
  "to_owner_id": 42
}
```

**Errors**

- `GUEST_MESSAGE_TYPE_NOT_ALLOWED` — type ≠ `CHAT`
- `PERMISSION_MANUAL_DISABLED` — guest attempted `PERMISSION`
- `GUEST_NOT_AUTHORIZED_FOR_ZONE` — zone not in JWT or recipient not a staff peer
- `PEERS_NOT_AVAILABLE` — no staff peers returned for the zone (POST path)

---

## `GET /api/guest/zones/{zone_id}/dashboard`

Adds read-only **`map`** payload (no polygon materialization by default):

```json
{
  "status": "success",
  "data": {
    "zone_id": "ZN-DEMO",
    "label": "Demo zone",
    "welcome_text": "Welcome to the zone guest dashboard.",
    "links": [],
    "cells": ["8928308280fffff"],
    "map": {
      "center": { "lat": 37.8, "lng": -122.4 },
      "zoom": 14,
      "cells": ["8928308280fffff"],
      "bounds": { "south": 37.7, "north": 37.9, "east": -122.3, "west": -122.5 },
      "geojson": { "type": "FeatureCollection", "features": [] }
    }
  }
}
```

**Map source**: **`zones.h3_cells`**, optional rich config under **`zones.parameters.guest_map`**: `{ "center": {lat,lng}, "zoom", "bounds", "geojson" }`.

---

## Message record fields (summary)

| Field | PERMISSION (guest workflow) | CHAT (guest/member) |
|--------|---------------------------|----------------------|
| `zone_message_events.type` | `PERMISSION` | `CHAT` |
| `sender_id` | admin or schedule owner; null rare | member id or null (guest send) |
| `sender_guest_id` | null (guest id in `body_json`) | guest id when guest sends |
| `receiver_id` | usually null (broadcast-style) | peer owner id for guest→staff |
| `guest_access_session_id` | session PK when known | session PK when known |
| `body_json` | `guest_id`, `guest_request_id`, `guest_name`, `resolution?`, `guest_message?` | `guest_id`, `guest_name`, `zone_id`, … |
| `metadata_json` | `domain_event`, `flow`, … | `flow`, … |

Member↔member **`messages`** table rows are unchanged; they are separate from guest access events.

---

## Schema migration

- **`zone_message_events.guest_access_session_id`** (nullable `INTEGER`, FK → `guest_access_sessions.id`, `ON DELETE SET NULL`). Added in SQLAlchemy model and PostgreSQL **`init_db`** patch (`database.py`).

---

## Error codes (stable)

- `GUEST_MESSAGE_TYPE_NOT_ALLOWED`
- `GUEST_NOT_AUTHORIZED_FOR_ZONE`
- `PEERS_NOT_AVAILABLE`
- `PERMISSION_MANUAL_DISABLED`
- `THREAD_NOT_FOUND` — reserved; not emitted by current guest list (empty thread returns `200` + empty `items`).
