"""Registered home address geocoding for `owners.latitude / owners.longitude`."""
from __future__ import annotations

import logging
from datetime import datetime

from app.models import Owner
from app.services.geocoding_service import geocode_address_best_effort

logger = logging.getLogger(__name__)


def apply_owner_home_geocode(owner: Owner, *, force: bool = False) -> bool:
    """Populate or refresh home coordinates from `owner.address`.

    Uses best-effort geocoding (full address, then shorter fragments). When
    ``force`` is True (e.g. after a settings save), re-geocodes even when coords
    already exist; clears coords when geocoding fails.
    """
    if not force and owner.latitude is not None and owner.longitude is not None:
        return False

    coords = geocode_address_best_effort(owner.address)
    if coords is None:
        if force:
            owner.latitude = None
            owner.longitude = None
            owner.location_updated_at = None
            logger.info("Home geocode failed for owner %s address %r", owner.id, owner.address)
        return False

    lat, lng = coords
    owner.latitude = lat
    owner.longitude = lng
    owner.location_updated_at = datetime.utcnow()
    logger.info(
        "Geocoded owner %s home address %r to (%.6f, %.6f)",
        owner.id,
        owner.address,
        lat,
        lng,
    )
    return True


def get_owner_home_coordinates(owner: Owner) -> tuple[float, float] | None:
    """Return geocoded home coordinates from the owner row."""
    if owner.latitude is None or owner.longitude is None:
        return None
    return float(owner.latitude), float(owner.longitude)
