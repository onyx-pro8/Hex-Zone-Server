# Backend contract: Access Zone messaging (Hex Zone)

Audience: backend team owning this repo. Frontend: Hex-Zone-Client (React/Vite).

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
