# Backend contract: Access Zone messaging (Hex Zone)

Audience: backend team owning this repo. Frontend: Hex-Zone-Client (React/Vite).

## Guest Access Workflow Contract

### Canonical endpoints

- `GET /api/access/qr-tokens/primary?zone_id={zone_id}` -> get-or-create reusable primary token (`expires_at` is never used for this flow)
- `POST /api/access/qr-tokens/primary/rotate` -> revoke active primary token and issue a new one
- `POST /api/access/qr-tokens` and `GET /api/access/qr-tokens` remain supported for backward compatibility
- `POST /api/access/permission` (anonymous) -> submit guest request
- `GET /api/access/session/{guest_id}?zone_id={zone_id}` -> poll approval status (`PENDING | APPROVED | REJECTED`)
- `GET /api/access/guest-requests?zone_id={zone_id}` -> admin/member request list with envelope
- `POST /message-feature/access/guest-requests/{requestId}/approve` (`zone_id` query optional legacy echo)
- `POST /message-feature/access/guest-requests/{requestId}/reject` (`zone_id` query optional legacy echo)

### API shapes (frontend contract lock)

- `GET /api/access/qr-tokens/primary?zone_id={zone_id}` and `POST /api/access/qr-tokens/primary/rotate` both return:
  - `{"status":"success","data":{"id","zone_id","token_suffix","url","path_with_query","revoked_at","created_at","updated_at"}}`
- `POST /api/access/permission` returns:
  - `{"status":"success","data":{"status":"EXPECTED|UNEXPECTED","message","guest_id","zone_id"}}`
- `GET /api/access/session/{guest_id}?zone_id={zone_id}` returns:
  - `{"status":"success","data":{"status":"PENDING|APPROVED|REJECTED","message","exchange_code?","exchange_expires_at?"}}`
- `GET /api/access/guest-requests?zone_id={zone_id}` returns:
  - `{"status":"success","data":[{"id","guest_id","zone_id","guest_name","status","expectation","created_at","hid"}]}`
- `POST /message-feature/access/guest-requests/{requestId}/approve|reject` returns:
  - `{"status":"success","data":{"id","status":"APPROVED|REJECTED","zone_id","updated_at"}}`

### Policy constraints

- One `zone_id` has one active primary guest QR token (`is_primary=true`, `revoked_at is null`) at a time.
- Primary token is reusable and non-expiring (`expires_at = null`).
- `PERMISSION` messages are server-generated only on:
  - request submit
  - admin approve
  - admin reject
- Manual compose APIs reject `PERMISSION` with `PERMISSION_MANUAL_DISABLED`.
- Guest write messaging is `CHAT` only; all non-`CHAT` types return `GUEST_MESSAGE_TYPE_NOT_ALLOWED`.
- Guest may send `CHAT` only to zone host/administrator peers returned by `GET /api/guest/zones/{zone_id}/peers`.
- For primary-token mode, `expires_at` / `expires_in_hours` are not accepted (`PRIMARY_TOKEN_EXPIRY_NOT_ALLOWED`).
- Legacy `POST /api/access/qr-tokens` + `GET /api/access/qr-tokens` remain supported.
  - For primary mode (`is_primary=true`), token never expires (`expires_at=null`).
  - If caller provides `expires_at` or `expires_in_hours` with primary mode, API rejects with `PRIMARY_TOKEN_EXPIRY_NOT_ALLOWED`.

### Required error codes

- `INVALID_GUEST_TOKEN`
- `TOKEN_ZONE_MISMATCH`
- `PERMISSION_MANUAL_DISABLED`
- `GUEST_MESSAGE_TYPE_NOT_ALLOWED`
- `GUEST_NOT_AUTHORIZED_FOR_ZONE`
- `ACCESS_REQUEST_NOT_FOUND`

## 4.3 `GET /api/guest/zones/{zone_id}/peers` — required for Guest Messages UI

**Auth:** `Authorization: Bearer <guest_jwt>` (guest access token from `POST /api/access/guest-session`).

**Purpose:** Enumerate zone **hosts / staff** (`owners.id`) the guest may use for PERMISSION or CHAT threads (match PDF “administrator communicates with sender”). The React app blocks messaging when this list is empty.

**Success:** HTTP **200** when the guest JWT allows `zone_id` (zone appears in JWT `zone_ids`).

**Response shapes** (client normalizes):

- `{ "status": "success", "data": { "zone_id", "peers": [ … ] } }`

**Each peer item** includes:

- **`owner_id`** (integer JSON; same as **`with_owner_id`** / **`to_owner_id`** on `/api/guest/messages`), and
- **`display_name`**, **`role`**, **`can_receive_chat`**, etc.

### Server implementation (this codebase)

Peers are built in `app/services/guest_api_service.py` from:

1. Active administrators with **`owners.zone_id` == `{zone_id}`**
2. Active **`zones`** rows with **`zones.zone_id` == `{zone_id}`** → include **`zones.owner_id`** (zone hosts)

Guest `POST /api/guest/messages` enforces this same host/admin peer set.

---

*Other sections (actors, guest-session, member `POST /messages`, etc.) are documented in OpenAPI (`/docs`, `/redoc`) and `API.md`.*
