"""Nearest-neighbour fan-out for UNKNOWN alarm messages."""
from __future__ import annotations

from math import atan2, cos, radians, sin, sqrt

from sqlalchemy.orm import Session

from app.models import Owner

# Sender account tier → max recipients (nearest owners with a stored location).
FANOUT_LIMIT_BY_ACCOUNT_TYPE: dict[str, int] = {
    "private": 20,
    "private_plus": 50,
    "exclusive": 5,
    "enhanced": 20,
    "enhanced_plus": 1000,
}

UNKNOWN_RATE_LIMIT_SECONDS = 10


def unknown_fanout_limit(sender: Owner) -> int:
    """Return how many nearest owners an UNKNOWN may reach for this sender."""
    key = str(sender.account_type.value).strip().lower()
    return FANOUT_LIMIT_BY_ACCOUNT_TYPE.get(key, 20)


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_m = 6_371_000.0
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return earth_radius_m * c


def resolve_nearest_owner_ids_among(
    db: Session,
    *,
    origin_lat: float,
    origin_lon: float,
    candidate_owner_ids: list[int],
    limit: int,
) -> list[int]:
    """Sort ``candidate_owner_ids`` by distance from origin; take top ``limit``."""
    if limit <= 0 or not candidate_owner_ids:
        return []

    unique_ids = list(dict.fromkeys(int(oid) for oid in candidate_owner_ids))
    rows = (
        db.query(Owner.id, Owner.latitude, Owner.longitude)
        .filter(
            Owner.id.in_(unique_ids),
            Owner.active.is_(True),
            Owner.latitude.isnot(None),
            Owner.longitude.isnot(None),
        )
        .all()
    )
    scored: list[tuple[float, int]] = []
    for owner_id, lat, lon in rows:
        distance = haversine_meters(origin_lat, origin_lon, float(lat), float(lon))
        scored.append((distance, int(owner_id)))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [owner_id for _, owner_id in scored[:limit]]


def resolve_nearest_owner_ids(
    db: Session,
    *,
    origin_lat: float,
    origin_lon: float,
    sender_id: int,
    limit: int,
) -> list[int]:
    """Active owners with coordinates, sorted by distance from origin; take top ``limit``."""
    if limit <= 0:
        return []

    rows = (
        db.query(Owner.id, Owner.latitude, Owner.longitude)
        .filter(
            Owner.active.is_(True),
            Owner.id != sender_id,
            Owner.latitude.isnot(None),
            Owner.longitude.isnot(None),
        )
        .all()
    )
    candidate_ids = [int(owner_id) for owner_id, _, _ in rows]
    return resolve_nearest_owner_ids_among(
        db,
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        candidate_owner_ids=candidate_ids,
        limit=limit,
    )
