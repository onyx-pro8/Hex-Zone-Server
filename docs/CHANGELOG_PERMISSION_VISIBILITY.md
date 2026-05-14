# PERMISSION visibility (merged member inbox)

## `ZoneMessageResponse` additions

- **`permission_visibility`**: `direct` | `zone_pending_broadcast` | omitted. Only meaningful when **`type`** is **`PERMISSION`** (persisted **`ZoneMessageEvent`**).
- **`guest_access_session_id`**: internal **`guest_access_sessions.id`** when the row references a session.
- **`session_pending`**: when **`permission_visibility`** is **`zone_pending_broadcast`**, indicates whether the linked session is still **`unexpected`** + **`pending`**.

## Semantics

- **`direct`**: merged **`GET /messages`** includes the row only for viewers whose **`owners.id`** equals **`sender_id`** or **`receiver_id`**. Legacy **`PERMISSION`** rows with **`receiver_id`** null are excluded from the merged feed.
- **`zone_pending_broadcast`**: first “awaiting approval” audit for an **unexpected** guest while **`GuestAccessSession.resolution`** is **`pending`** is visible to every staff member who passes **`guest_access_service.can_manage_zone_guest_requests`** for that zone. After the session is no longer unexpected+pending, the row is only visible under the **`direct`** rule (same **`sender_id`** / **`receiver_id`**).

Guest **`GET /api/guest/messages`** is unchanged for threading; **`raw_payload`** may include **`permission_visibility`** for debugging.

## WebSockets

**`PERMISSION_MESSAGE`** for guest pass lifecycle targets only the paired **`delivered_owner_ids`** (requester + counterparty, or reviewer + requester, etc.), not full zone staff. **`unexpected_guest`** remains zone-wide for operational alert.
