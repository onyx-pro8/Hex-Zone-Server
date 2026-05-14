"""Server-side PERMISSION row visibility for merged member inbox (`ZoneMessageEvent` type PERMISSION)."""

from __future__ import annotations

from typing import Literal

PERMISSION_VISIBILITY_DIRECT = "direct"
PERMISSION_VISIBILITY_ZONE_PENDING_BROADCAST = "zone_pending_broadcast"

PermissionVisibility = Literal["direct", "zone_pending_broadcast"]
