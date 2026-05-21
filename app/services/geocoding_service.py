"""Forward geocoding helpers used to populate `owners.latitude/longitude`.

The dynamic-zone resolver and the `/me` endpoint both expect every owner to
expose a canonical map center on `owners.latitude / owners.longitude`. Users
provide a postal address at registration but no coordinates, so we resolve the
address to a `(lat, lon)` pair via OpenStreetMap Nominatim with a Photon
fallback. Failures are swallowed and return `None`; callers must handle
`None` (typically by leaving the columns NULL and trying again on the next
login / location update).

This module intentionally lives alongside `area_boundary_service` so we can
reuse its rate-limited HTTP client and shared User-Agent header.

All network calls use **hard timeouts** (a few seconds) because the resolvers
run inside request handlers — a 30 s timeout would block the entire FastAPI
event loop and starve other requests.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.core.config import settings
from app.services.area_boundary_service import (
    NOMINATIM_SEARCH_URL,
    PHOTON_SEARCH_URL,
    _wait_nominatim_rate_limit,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 4.0


def _normalise_address(address: Optional[str]) -> str:
    if not address:
        return ""
    cleaned = " ".join(str(address).split()).strip()
    # The wizard seeds the placeholder "N/A" when no address is captured.
    if cleaned.upper() in {"", "N/A", "NA", "NONE"}:
        return ""
    return cleaned


def _nominatim_forward(address: str) -> Optional[tuple[float, float]]:
    """Forward geocode via Nominatim (`/search?q=…`). Returns (lat, lon).

    Uses a hard short timeout because the call is invoked from request
    handlers; we'd rather skip geocoding than block the event loop.
    """
    try:
        _wait_nominatim_rate_limit()
        ua = getattr(settings, "NOMINATIM_USER_AGENT", "HexZone/1.0")
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = client.get(
                NOMINATIM_SEARCH_URL,
                params={
                    "q": address,
                    "format": "json",
                    "limit": 1,
                    "addressdetails": 0,
                },
                headers={"User-Agent": ua, "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:  # pragma: no cover - network failure path
        logger.warning("Nominatim forward geocode failed for %r: %s", address, exc)
        return None
    if not isinstance(payload, list) or not payload:
        return None
    row = payload[0]
    if not isinstance(row, dict):
        return None
    try:
        lat = float(row.get("lat"))
        lon = float(row.get("lon"))
    except (TypeError, ValueError):
        return None
    return lat, lon


def _photon_forward(address: str) -> Optional[tuple[float, float]]:
    """Forward geocode via Photon (Komoot). Returns (lat, lon).

    Mirrors `area_boundary_service._photon_geocode` but only returns the
    coordinates because callers do not need the formatted label. Uses a hard
    short timeout for the same reason as `_nominatim_forward`.
    """
    try:
        ua = getattr(settings, "NOMINATIM_USER_AGENT", "HexZone/1.0")
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = client.get(
                PHOTON_SEARCH_URL,
                params={"q": address, "limit": 1, "lang": "en"},
                headers={"User-Agent": ua, "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:  # pragma: no cover - network failure path
        logger.warning("Photon forward geocode failed for %r: %s", address, exc)
        return None
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list) or not features:
        return None
    feature = features[0]
    if not isinstance(feature, dict):
        return None
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    try:
        lon = float(coordinates[0])
        lat = float(coordinates[1])
    except (TypeError, ValueError):
        return None
    return lat, lon


def geocode_address(address: Optional[str]) -> Optional[tuple[float, float]]:
    """Resolve a free-text address to `(lat, lon)`.

    Order of resolvers:
      1. Nominatim (rate-limited, shared with area-boundary lookups).
      2. Photon (Komoot mirror) as a fallback when Nominatim returns nothing.

    Returns ``None`` when both resolvers fail or the address is empty / "N/A".
    Callers must tolerate ``None``; this helper is fire-and-forget and must
    never raise.
    """
    cleaned = _normalise_address(address)
    if not cleaned:
        return None

    coords = _nominatim_forward(cleaned)
    if coords is not None:
        return coords

    coords = _photon_forward(cleaned)
    if coords is not None:
        return coords

    logger.info("No geocoding match found for address %r", cleaned)
    return None
