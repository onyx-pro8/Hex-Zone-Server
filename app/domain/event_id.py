"""Canonical **event_id** handling for guest access (schedules, guest passes, QR tokens).

Guests may send the value from the invite query (**`eid`**) as-is (trimmed only). Hosts may
register schedules or guest passes with a prefixed form (e.g. ``EVT-1234``) while the guest
sends only the numeric part (**``1234``**). Those are treated as the same logical event for
matching. Other identifiers are compared in a Unicode case-insensitive way (``casefold``).
"""

from __future__ import annotations

import re
from typing import Final

# ``EVT`` / ``evt`` plus optional separator and a purely numeric rest → canonical key is the digit string.
_EVT_NUMERIC: Final = re.compile(r"^evt[-_]?(\d+)$", re.IGNORECASE)


def canonical_event_id(value: str | None) -> str | None:
    """Return a normalized key for access matching, or ``None`` if empty after trim."""
    if value is None:
        return None
    t = value.strip()
    if not t:
        return None
    m = _EVT_NUMERIC.match(t)
    if m:
        return m.group(1)
    if t.isdigit():
        return t
    return t.casefold()


def event_ids_equivalent(a: str | None, b: str | None) -> bool:
    """True when **a** and **b** denote the same logical event for access checks."""
    return canonical_event_id(a) == canonical_event_id(b)


def event_id_lowercase_sql_in_values(canon: str) -> list[str]:
    """Lowercase strings for ``func.lower(column).in_(...)`` so DB values match **canon**."""
    if not canon:
        return []
    if canon.isdigit():
        return sorted({canon, f"evt-{canon}", f"evt_{canon}", f"evt{canon}"})
    return [canon]
