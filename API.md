# Guest access API (approved anonymous guests)

Base URL is the same as the rest of the server (e.g. `https://api.example.com`). Paths below are absolute from the server root.

**OpenAPI (Swagger):** interactive docs at **`/docs`** (or **`/redoc`**). Guest and access routes use **`response_model`** so request/response schemas, field descriptions, and error models (**`GuestApiHttpError`**, **`GuestAccessHttpError`**, **`GuestRequestListEnvelope`**, **`GuestAccessSessionListItem`**) appear in the UI. Source: `app/schemas/guest_api.py`, `app/schemas/access_guest.py`, `app/schemas/schemas.py`, `app/routers/guest.py`, `app/routers/access.py`, `app/routers/messages.py`.

## Envelope

**Success**

```json
{ "status": "success", "data": { } }
```

**Error** (HTTP 4xx/5xx; handler always includes `error`)

```json
{
  "status": "error",
  "message": "human readable",
  "error_code": "STABLE_CODE",
  "error": { "message": "same or more specific text" }
}
```

Common HTTP codes: **400** validation, **401** auth, **403** forbidden, **404** not found, **409** conflict (e.g. consumed exchange), **429** rate limit.

---

## Member: `GET /api/access/guest-requests`

**Auth:** `Authorization: Bearer <member JWT>` (same stack as `/messages`, `/zones`).

**Query**

| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `zone_id` | yes | — | Shared Hex zone id (**zid**). |
| `status` | no | — | Filter **PENDING**, **APPROVED**, **REJECTED** (also **GRANTED** / **DENIED**). |
| `pending_only` | no | false | If true, only unexpected sessions still **pending**. |
| `limit` | no | 50 | 1–200. |
| `skip` | no | 0 | Pagination offset. |

**200** body: `{ "status": "success", "data": [ GuestAccessSessionListItem, … ] }` — newest **`created_at`** first. Each row includes **`guest_id`**, **`zone_id`**, **`guest_name`**, **`guest_status`**, **`status`** (approval UI), **`expectation`** (**expected** \| **unexpected**), **`created_at`**, optional **`device_id`** / **`hid`**, etc. Same **`guest_id`** as `POST /api/access/permission` and as `POST /messages` when messaging that guest.

**401** if bearer missing/invalid. **403** if the caller cannot administer the zone (account visibility rules). **404** if the authenticated owner row is missing (rare).

**Legacy:** `GET /message-feature/access/guest-requests?zone_id=` returns the same rows as a **bare JSON array** (no envelope).

---

## Member: `POST /messages` (guest thread)

**Auth:** member Bearer.

Use the core **`POST /messages`** route (or contract **`POST /messages`** with **`ChatMessageCreateRequest`**) with:

| Field | Required | Description |
|-------|----------|-------------|
| `message` | yes | Body text (**CHAT** must be non-empty after trim). |
| `type` or `message_type` | yes\* | **PERMISSION** or **CHAT** only for guest threads. |
| `visibility` | yes\* | Typically **private** (maps scope); \*same rules as normal chat if legacy-only. |
| `zone_id` or `zoneId` | yes | Must match the guest session’s zone. |
| `guest_id` | yes | Opaque id from listing or permission flow. |

Do **not** send **`receiver_id`**. Persists **`ZoneMessageEvent`**; the guest reads it on **`GET /api/guest/messages`** with **`with_owner_id`** = the member’s **`owners.id`**.

**201** returns **`ZoneMessageResponse`** with string UUID **`id`** (event id). **403** / **404** / **422** with structured **`detail`** (`FORBIDDEN`, `GUEST_NOT_FOUND`, `MISSING_ZONE_FOR_GUEST`, etc.).

---

## Extended: `GET /api/access/session/{guest_id}`

**Auth:** none.

**Query**

| Param     | Required | Description                                      |
|-----------|----------|--------------------------------------------------|
| `zone_id` | optional | If set, must match the session’s stored zone.   |

**Response** (unchanged shape; optional fields only when `status` is `APPROVED` and a valid unused exchange exists)

- `guest_id`, `zone_id`, `status`, `message` — as before.
- `exchange_code` — UUID string; **omit** when not `APPROVED`, consumed, or expired.
- `exchange_expires_at` — ISO-8601 UTC (e.g. `2026-05-03T12:00:00Z`); only with `exchange_code`.

---

## `POST /api/access/guest-session`

**Auth:** none (do not send `Authorization`).

**Rate limit:** per client IP, rolling 1 minute (`GUEST_ACCESS_GUEST_SESSION_MAX_PER_MINUTE`, default 30).

**Body (JSON)**

| Field          | Type   | Required |
|----------------|--------|----------|
| `guest_id`     | string | yes      |
| `zone_id`      | string | yes      |
| `exchange_code`| string | yes      |
| `device_id`    | string | no       |

If the guest session has a persisted `device_id` from `POST /api/access/permission` and the client sends a different non-empty `device_id`, the server returns **403** `device_mismatch`.

**Success `data`**

| Field           | Description |
|-----------------|-------------|
| `access_token`  | JWT (`guest_access`, see claims below). |
| `token_type`    | `"Bearer"` |
| `expires_in`    | Seconds (from `GUEST_ACCESS_TOKEN_EXPIRE_MINUTES`, default 3600). |
| `guest`         | `{ guest_id, display_name, zone_ids[], allowed_message_types: ["PERMISSION","CHAT"] }` |

**Side effect:** `exchange_code` is consumed (single use).

**Error codes**

| `error_code`        | HTTP | Meaning |
|---------------------|------|---------|
| `exchange_invalid`  | 400/404 | Wrong or unknown code / bad input. |
| `exchange_expired`  | 400 | TTL elapsed (`GUEST_ACCESS_EXCHANGE_TTL_MINUTES`, default 12 min from approve). |
| `exchange_consumed` | 409 | Code already used. |
| `guest_not_approved`| 403 | Session not approved. |
| `zone_mismatch`     | 403 | `zone_id` ≠ session zone. |
| `device_mismatch`   | 403 | Optional device binding failed. |
| `NOT_FOUND`         | 404 | Unknown `guest_id` session. |
| `RATE_LIMITED`      | 429 | Too many attempts. |

---

## Guest JWT

Claims (HS256, same `SECRET_KEY` / `ALGORITHM` as member JWT):

- `sub`: `guest:{guest_id}`
- `token_use` / `typ`: `guest_access`
- `zone_ids`: array of zone id strings the guest may use
- `allowed_message_types`: `["PERMISSION","CHAT"]`
- `exp`, `iat`, `jti`

**Important:** Member routes that expect `sub` as numeric owner id must reject guest tokens (they will fail validation). Only **`/api/guest/*`** should accept this token.

---

## `GET /api/guest/me`

**Auth:** `Authorization: Bearer <access_token>` (guest token).

**Success `data`**

- `guest_id`, `display_name`, `zone_ids`, `allowed_message_types`
- `expires_at` — ISO-8601 UTC from JWT `exp`

**401** if token missing/invalid/wrong `token_use`.

---

## `GET /api/guest/zones/{zone_id}/peers`

**Auth:** guest Bearer. **403** if `zone_id` not in JWT `zone_ids`.

**Success `data`**

- `zone_id`
- `peers`: `[ { peer_kind: "owner", owner_id, display_name, role, can_receive_chat } ]`  
  Active owners in the zone; `can_receive_chat` reflects message-type blocks for **CHAT**.

---

## `GET /api/guest/zones/{zone_id}/dashboard` (optional)

**Auth:** guest Bearer; zone must be allowed.

**Success `data`:** minimal safe payload (`zone_id`, `label`, `welcome_text`, `links`).

---

## `GET /api/guest/messages`

**Auth:** guest Bearer.

**Query**

| Param            | Required | Default | Description |
|------------------|----------|---------|-------------|
| `zone_id`        | yes      | —       | Must be in JWT `zone_ids`. |
| `with_owner_id`  | no       | —       | Filter to thread with that owner. |
| `limit`          | no       | 50      | Max 200. |
| `cursor`         | no       | —       | Opaque pagination from prior `next_cursor`. |
| `before_id`      | no       | —       | Event id anchor for older page. |

**Success `data`**

- `items`: only **`PERMISSION`** and **`CHAT`** events involving this `guest_id` (including `sender_guest_id` and body `guest_id` linkage).
- `next_cursor`: optional string.

---

## `POST /api/guest/messages`

**Auth:** guest Bearer.

**CHAT body**

```json
{
  "zone_id": "string",
  "type": "CHAT",
  "text": "string",
  "to_owner_id": 123
}
```

**PERMISSION body** (optional support)

```json
{
  "zone_id": "string",
  "type": "PERMISSION",
  "text": "optional string",
  "to_owner_id": 123,
  "msg": { "guest_name": "string", "event_id": "optional string" }
}
```

**403** `forbidden_message_type` for any other `type`.

Persists `ZoneMessageEvent` with `sender_guest_id` set; `receiver_id` = `to_owner_id`. Delivery respects the same **message-type block** rules as members (recipient with block on `CHAT` / `PERMISSION` cannot receive).

**201** success `data`: one message object (same shape as `items[]` in GET).

---

## WebSocket

**v1:** REST polling only. A future revision may document `WSS` subscription using the guest JWT.

---

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `GUEST_ACCESS_EXCHANGE_TTL_MINUTES` | 12 | Exchange code lifetime after approve. |
| `GUEST_ACCESS_TOKEN_EXPIRE_MINUTES` | 60 | Guest JWT TTL (`expires_in`). |
| `GUEST_ACCESS_GUEST_SESSION_MAX_PER_MINUTE` | 30 | `POST /api/access/guest-session` per IP. |

PostgreSQL deployments also get `ALTER TABLE … IF NOT EXISTS` patches at startup for `exchange_*` and `sender_guest_id` (see `app/database.py`). Alembic revision **`008_guest_exchange`** mirrors the same.
