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

### Policy constraints

- One `zone_id` has one active primary guest QR token (`is_primary=true`, `revoked_at is null`) at a time.
- Primary token is reusable and non-expiring (`expires_at = null`).
- `PERMISSION` messages are server-generated only on:
  - request submit
  - admin approve
  - admin reject
- Manual compose APIs reject `PERMISSION` with `PERMISSION_MANUAL_DISABLED`.
- Guest write messaging is `CHAT` only; all non-`CHAT` types return `GUEST_MESSAGE_TYPE_NOT_ALLOWED`.
- For primary-token mode, `expires_at` / `expires_in_hours` are not accepted (`PRIMARY_TOKEN_EXPIRY_NOT_ALLOWED`).

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

Peers are built from **`zone_staff_owner_ids`** in `app/services/guest_access_service.py`:

1. Active owners with **`owners.zone_id` == `{zone_id}`**
2. Active **`zones`** rows with **`zones.zone_id` == `{zone_id}`** → include **`zones.owner_id`**
3. **`resolve_primary_zone_admin_owner`** so a zone administrator is listed even when membership is split across **`owners`** vs **`zones`**

`list_zone_peers_for_guest` in `app/services/guest_api_service.py` turns those ids into the **`GuestPeerItem`** list.

---

*Other sections (actors, guest-session, member `POST /messages`, etc.) are documented in OpenAPI (`/docs`, `/redoc`) and `API.md`.*
