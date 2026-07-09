# Zones API capability contract

This backend enforces per-user zone capacity (by `zones.creator_id`) and edit authorization in server-side policy.

## Policy defaults

- `MAX_ZONES_TOTAL=3` (each user may create up to **3 zones**)
- `RESERVED_FOR_STANDARD_USERS` is retained in config for compatibility but is **not** applied to the per-user quota

## Edit authorization

Option A is enforced: a caller may edit only zones they created (`creator_id == caller.id`).

## Naming policy

- `name` is required on create.
- `name` is trimmed before persistence.
- Valid length is `1..120`.
- Name must be unique within the account scope (administrator + linked users), case-insensitive.

## Capabilities endpoint

`GET /zones/capabilities` returns:

```json
{
  "role": "administrator",
  "can_create_zone": false,
  "remaining_total": 0,
  "remaining_for_role": 0,
  "max_total": 3,
  "reserved_for_standard_users": 0,
  "reason": "Maximum of 3 zones per user reached."
}
```
