# Zones API capability contract

This backend enforces per-creator zone capacity, primary/secondary tiering, and
edit/delete authorization in server-side policy.

## Policy defaults

- `MAX_ZONES_ADMINISTRATOR=3` — account administrators may create up to **3 zones total**
- `MAX_ZONES_ADMINISTRATOR_PRIMARY=2` — of those, up to **2 are primary**; additional admin zones are **secondary**
- Invited members create **secondary zones only**
- Member secondary cap = `MAX_ZONES_ADMINISTRATOR - admin_primary_count`
  - 1 admin primary → each member may create **2** secondary zones
  - 2 admin primaries → each member may create **1** secondary zone
- Create quota counts **active** zones only. Soft-deleting a zone frees a create slot.
- Active primary count (only) drives member secondary caps and messaging visibility.
- Listing visibility:
  - **Primary** zones: visible to the account administrator and all members
  - **Secondary** zones: visible only to the creator
  - Map and zone list are **network-scoped only** (up to account quota). Communal
    selection uses `GET /zones/public`, which returns only **primary** zones in the
    caller's network (not other networks).
  - System administrator listing/edit rules are unchanged (sees all zones)

## Edit / delete authorization

- **Primary** zones: modified and removed by the **account administrator** only
- **Secondary** zones: modified and removed by the **creator** only
- System administrators retain full access
- Administrators may **choose** primary vs secondary on create when both slots remain (`is_primary` on create payload / Zone tier UI)

## Naming policy

- `name` is required on create.
- `name` is trimmed before persistence.
- Valid length is `1..120`.
- Name must be unique among **active** zones within the account scope (administrator + linked users), case-insensitive. Soft-deleted names may be reused.

## Capabilities endpoint

`GET /zones/capabilities` returns:

```json
{
  "role": "administrator",
  "can_create_zone": true,
  "remaining_total": 1,
  "remaining_for_role": 1,
  "max_total": 3,
  "max_primary": 2,
  "admin_primary_count": 1,
  "next_zone_is_primary": true,
  "member_secondary_limit": 2,
  "reserved_for_standard_users": 2,
  "reason": null
}
```

Create responses may include `evicted_zones` when member secondaries were trimmed. Affected users also receive a `ZONE_EVICTED` WebSocket event (and push when available).
