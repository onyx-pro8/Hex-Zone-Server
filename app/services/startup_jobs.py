"""Long-running startup jobs that must not block FastAPI request handlers.

Currently this hosts the owner-address geocoding backfill: existing owners
created before the registration-time geocoder ran (or owners whose first
geocoding attempt failed) still have NULL `latitude / longitude`. We resolve
them in a dedicated background thread so:

* The application is up immediately — health checks and request handlers do
  not wait for Nominatim.
* Nominatim's 1 req/sec rate-limit is respected — we use the shared limiter in
  `area_boundary_service` and add an extra sleep between owners as defence in
  depth.
* Failures are silent and the job moves on; the next deploy / restart will
  retry any owner whose address still didn't resolve.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import session_maker
from app.models import Owner
from app.services.geocoding_service import geocode_address

logger = logging.getLogger(__name__)

# Conservative pacing on top of the Nominatim 1 req/sec limiter; keeps the
# backfill polite on free-tier OpenStreetMap infrastructure even when the
# limiter window has rolled over.
_PER_OWNER_DELAY_SECONDS = 1.2
_MAX_OWNERS_PER_RUN = 200


def _select_owners_needing_geocode(db: Session) -> Iterable[Owner]:
    return (
        db.query(Owner)
        .filter(or_(Owner.latitude.is_(None), Owner.longitude.is_(None)))
        .filter(Owner.address.isnot(None))
        .order_by(Owner.id.asc())
        .limit(_MAX_OWNERS_PER_RUN)
        .all()
    )


def backfill_owner_coordinates() -> None:
    """Resolve `owners.latitude/longitude` for rows that still lack a fix.

    Designed to be invoked from a daemon thread at app startup. The function
    catches its own exceptions so a single bad address (or a Nominatim outage)
    cannot terminate the worker thread.
    """
    db = session_maker()
    try:
        owners = _select_owners_needing_geocode(db)
        if not owners:
            logger.info("Owner geocode backfill: nothing to do")
            return
        logger.info("Owner geocode backfill: %d owner(s) queued", len(owners))
        resolved = 0
        for owner in owners:
            try:
                coords = geocode_address(owner.address)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Geocode raised for owner %s: %s", owner.id, exc)
                coords = None
            if coords is not None:
                lat, lng = coords
                owner.latitude = lat
                owner.longitude = lng
                owner.location_updated_at = datetime.utcnow()
                resolved += 1
                try:
                    db.commit()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Commit failed for owner %s: %s", owner.id, exc)
                    db.rollback()
            time.sleep(_PER_OWNER_DELAY_SECONDS)
        logger.info(
            "Owner geocode backfill complete: %d resolved / %d queued",
            resolved,
            len(owners),
        )
    finally:
        db.close()
