"""Geospatial evaluation using H3, dynamic clusters, and PostGIS-compatible data."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from math import atan2, cos, radians, sin, sqrt
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.h3_utils import lat_lng_to_h3_cell
from app.models import Owner, Zone

# Equirectangular projection scale at the equator. Acceptable inaccuracy for
# clusters within ~100 km of the projection reference latitude (sub-percent).
_METERS_PER_DEG_LAT = 111_320.0

# Hard cap on candidate population per dynamic resolution. Above this size the
# O(U^2 log U) nearest-neighbor scan becomes too slow; real zones rarely have
# more linked active members than this and the cap protects API latency.
_MAX_POPULATION_FOR_CLUSTER_SCAN = 1500


def evaluate_member_zones(
    db: Session,
    latitude: float,
    longitude: float,
    candidate_owner_ids: Iterable[int],
    *,
    include_dynamic_zones: bool = True,
) -> list[str]:
    owner_ids = list(candidate_owner_ids)
    if not owner_ids:
        return []

    h3_cell = lat_lng_to_h3_cell(latitude, longitude, 13)
    h3_sql = text(
        """
        SELECT z.zone_id
        FROM zones z
        WHERE z.owner_id = ANY(:owner_ids)
          AND z.active = TRUE
          AND z.h3_cells IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM json_array_elements_text(z.h3_cells) AS cell
              WHERE cell = :h3_cell
          )
        """
    )
    matched = {
        row[0]
        for row in db.execute(
            h3_sql,
            {"owner_ids": owner_ids, "h3_cell": h3_cell},
        )
    }

    postgis_sql = text(
        """
        SELECT z.zone_id
        FROM zones z
        WHERE z.owner_id = ANY(:owner_ids)
          AND z.active = TRUE
          AND z.geo_fence_polygon IS NOT NULL
          AND ST_Contains(
              z.geo_fence_polygon,
              ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)
          )
        """
    )
    for row in db.execute(postgis_sql, {"owner_ids": owner_ids, "longitude": longitude, "latitude": latitude}):
        matched.add(row[0])

    # Non-polygon circle-based zone types are evaluated from stored geometry/config.
    circle_candidates = (
        db.query(Zone)
        .filter(Zone.owner_id.in_(owner_ids), Zone.active.is_(True))
        .all()
    )
    for zone in circle_candidates:
        if include_dynamic_zones and _point_in_dynamic_zone(zone, latitude, longitude):
            matched.add(zone.zone_id)
            continue
        if _point_in_proximity_zone(zone, latitude, longitude):
            matched.add(zone.zone_id)
            continue
        if _point_in_object_zone(db, zone, latitude, longitude, owner_ids):
            matched.add(zone.zone_id)

    return sorted(matched)


def evaluate_zones_containing_point(db: Session, latitude: float, longitude: float) -> list[str]:
    """Return zone_ids whose geometry contains ``(latitude, longitude)`` across all active owners.

    Used for cross-account message propagation: any owner whose acceptable zone
    (a zone they own) includes the message coordinates may receive the alarm.
    """
    active_owner_rows = db.query(Owner.id).filter(Owner.active.is_(True)).all()
    owner_ids = [int(row[0]) for row in active_owner_rows]
    return evaluate_member_zones(
        db,
        latitude,
        longitude,
        owner_ids,
        include_dynamic_zones=False,
    )


def owner_ids_whose_acceptable_zones_contain_point(
    db: Session,
    latitude: float,
    longitude: float,
) -> tuple[list[str], list[int]]:
    """Owners whose owned zones contain the point (acceptable-zone match)."""
    zone_ids = evaluate_zones_containing_point(db, latitude, longitude)
    if not zone_ids:
        return [], []

    rows = (
        db.query(Zone.owner_id)
        .filter(Zone.zone_id.in_(zone_ids), Zone.active.is_(True))
        .distinct()
        .all()
    )
    owner_ids = sorted({int(row[0]) for row in rows})
    return zone_ids, owner_ids


def _point_in_dynamic_zone(zone: Zone, latitude: float, longitude: float) -> bool:
    """Match point against the saved resolved radius for a dynamic zone.

    A dynamic zone is a circle whose radius is **resolved on save** so it contains a
    target number of nearest users within the [min, max] band. The persisted
    `resolved_radius_meters` is the authoritative inclusion radius. Legacy zones that
    pre-date the resolved-radius shape fall back to the saved `max_radius_meters` to
    avoid silently shrinking to zero.
    """
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    contract_type = str(params.get("contractType") or "").strip().lower()
    if contract_type != "dynamic":
        return False

    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    config = params.get("config") if isinstance(params.get("config"), dict) else {}

    resolved_radius = _coerce_positive_number(config.get("resolved_radius_meters"))
    fallback_radius = _coerce_positive_number(config.get("max_radius_meters"))
    inclusion_radius = resolved_radius or fallback_radius
    if inclusion_radius is None:
        return False

    center = geometry.get("center")
    if isinstance(center, dict):
        c_lat = center.get("latitude")
        c_lng = center.get("longitude")
        if isinstance(c_lat, (int, float)) and isinstance(c_lng, (int, float)):
            distance = _haversine_meters(latitude, longitude, float(c_lat), float(c_lng))
            if distance <= inclusion_radius:
                return True

    centers = geometry.get("centers")
    if isinstance(centers, list):
        for item in centers:
            if not isinstance(item, dict):
                continue
            c_lat = item.get("latitude")
            c_lng = item.get("longitude")
            if not isinstance(c_lat, (int, float)) or not isinstance(c_lng, (int, float)):
                continue
            distance = _haversine_meters(latitude, longitude, float(c_lat), float(c_lng))
            if distance <= inclusion_radius:
                return True
    return False


def _coerce_positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


@dataclass
class DynamicZoneResolution:
    """Outcome of resolving a dynamic zone from population + radius bounds.

    Either describes a feasible cluster (`infeasible=False`) with a server-picked
    center+radius and the member ids inside that disk, or reports the reason no
    cluster of `target_user_count` users fits inside `max_radius_meters`.
    """

    target_user_count: int
    min_radius_meters: float
    max_radius_meters: float
    population_size: int
    infeasible: bool = False
    reason: Optional[str] = None
    center_latitude: Optional[float] = None
    center_longitude: Optional[float] = None
    resolved_radius_meters: Optional[float] = None
    matched_owner_ids: list[int] = field(default_factory=list)
    tight_radius_meters: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "infeasible": self.infeasible,
            "reason": self.reason,
            "center": (
                {
                    "latitude": self.center_latitude,
                    "longitude": self.center_longitude,
                }
                if self.center_latitude is not None and self.center_longitude is not None
                else None
            ),
            "resolved_radius_meters": self.resolved_radius_meters,
            "tight_radius_meters": self.tight_radius_meters,
            "matched_user_count": len(self.matched_owner_ids),
            "matched_owner_ids": list(self.matched_owner_ids),
            "population_size": self.population_size,
            "target_user_count": self.target_user_count,
            "min_radius_meters": self.min_radius_meters,
            "max_radius_meters": self.max_radius_meters,
        }


def resolve_dynamic_zone_cluster(
    db: Session,
    *,
    zone_id: str,
    target_user_count: int,
    min_radius_meters: float,
    max_radius_meters: float,
    exclude_owner_ids: Iterable[int] = (),
) -> DynamicZoneResolution:
    """Pick a disk that covers the tightest cluster of N nearest users in a zone.

    No caller-supplied center: for every active owner of `zone_id` whose
    `owners.latitude / longitude` is set, take that owner plus their N-1 nearest
    neighbors, compute the smallest enclosing circle of the group, keep
    clusters whose tight radius is `<= max_radius_meters`, clamp the radius to
    `[min, max]`, and return the cluster with the smallest effective radius.
    Returns an infeasible result when no such cluster exists so the UI can show
    a "not found" message.
    """
    if target_user_count < 1:
        raise ValueError("target_user_count must be >= 1")
    if min_radius_meters <= 0:
        raise ValueError("min_radius_meters must be > 0")
    if max_radius_meters < min_radius_meters:
        raise ValueError("max_radius_meters must be >= min_radius_meters")

    excluded = {oid for oid in exclude_owner_ids if isinstance(oid, int)}
    # Canonical location source is `owners.latitude / longitude`. Owners without
    # a location yet are silently skipped (NULL filter) so they never anchor the
    # cluster but also never break feasibility for the rest of the population.
    rows = (
        db.query(Owner.id, Owner.latitude, Owner.longitude)
        .filter(
            Owner.zone_id == zone_id,
            Owner.active.is_(True),
            Owner.latitude.isnot(None),
            Owner.longitude.isnot(None),
        )
        .all()
    )
    population: list[tuple[int, float, float]] = []
    for owner_id, lat, lng in rows:
        if owner_id in excluded:
            continue
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            continue
        population.append((int(owner_id), float(lat), float(lng)))

    base = DynamicZoneResolution(
        target_user_count=target_user_count,
        min_radius_meters=float(min_radius_meters),
        max_radius_meters=float(max_radius_meters),
        population_size=len(population),
    )

    if not population:
        base.infeasible = True
        base.reason = (
            "No active users in this zone have a stored location on their owner record yet."
        )
        return base
    if len(population) < target_user_count:
        base.infeasible = True
        base.reason = (
            f"Only {len(population)} users with a stored location are available; "
            f"need at least {target_user_count}."
        )
        return base
    if len(population) > _MAX_POPULATION_FOR_CLUSTER_SCAN:
        base.infeasible = True
        base.reason = (
            f"Zone has {len(population)} active users; cluster search is capped "
            f"at {_MAX_POPULATION_FOR_CLUSTER_SCAN}. Reduce the population or use a different zone type."
        )
        return base

    # Project to local equirectangular meters around the population centroid so
    # SEC math runs in a plane. Accuracy is sub-percent for clusters within tens
    # of km of the centroid, which is well inside the supported max_radius.
    lat0 = sum(p[1] for p in population) / len(population)
    lng0 = sum(p[2] for p in population) / len(population)
    meters_per_deg_lon = max(_METERS_PER_DEG_LAT * cos(radians(lat0)), 1e-6)

    projected: list[tuple[int, float, float]] = []
    for owner_id, lat, lng in population:
        x = (lng - lng0) * meters_per_deg_lon
        y = (lat - lat0) * _METERS_PER_DEG_LAT
        projected.append((owner_id, x, y))

    best_key: Optional[tuple[float, int, float, int]] = None
    best_result: Optional[tuple[float, float, float, float, list[int]]] = None

    for anchor_idx, (_anchor_id, ax, ay) in enumerate(projected):
        ranked: list[tuple[float, int, float, float]] = []
        for owner_id, x, y in projected:
            dx = x - ax
            dy = y - ay
            ranked.append((dx * dx + dy * dy, owner_id, x, y))
        ranked.sort(key=lambda row: (row[0], row[1]))
        chosen = ranked[:target_user_count]
        cluster_points = [(row[2], row[3]) for row in chosen]

        cx, cy, tight_r = _smallest_enclosing_circle(cluster_points)
        if tight_r > max_radius_meters:
            continue
        effective_r = max(tight_r, float(min_radius_meters))
        if effective_r > max_radius_meters:
            # Only reachable if min > max, which we validate against above.
            continue

        # Recount everything the resolved disk actually covers (may exceed N
        # when the cluster is tighter than min_radius_meters).
        matched_ids: list[int] = []
        r_sq = effective_r * effective_r
        for owner_id, x, y in projected:
            dxc = x - cx
            dyc = y - cy
            if dxc * dxc + dyc * dyc <= r_sq:
                matched_ids.append(owner_id)

        # Tie-break: smaller effective_r wins; then more matched users; then
        # tighter SEC; then anchor index for determinism.
        candidate_key = (effective_r, -len(matched_ids), tight_r, anchor_idx)
        if best_key is None or candidate_key < best_key:
            best_key = candidate_key
            best_result = (cx, cy, effective_r, tight_r, matched_ids)

    if best_result is None:
        base.infeasible = True
        base.reason = (
            f"No cluster of {target_user_count} users fits within {max_radius_meters:.0f} m."
        )
        return base

    cx, cy, effective_r, tight_r, matched_ids = best_result
    center_lat = lat0 + cy / _METERS_PER_DEG_LAT
    center_lng = lng0 + cx / meters_per_deg_lon

    base.center_latitude = center_lat
    base.center_longitude = center_lng
    base.resolved_radius_meters = effective_r
    base.tight_radius_meters = tight_r
    base.matched_owner_ids = sorted(matched_ids)
    return base


# ---------------------------------------------------------------------------
# Smallest enclosing circle (Welzl, randomized, expected O(n)).
# Adapted from Nayuki's reference implementation. Operates in planar
# coordinates; callers must project lat/lng to meters first.
# ---------------------------------------------------------------------------

_SEC_EPS_FACTOR = 1 + 1e-14


def _smallest_enclosing_circle(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    if not points:
        return (0.0, 0.0, 0.0)
    if len(points) == 1:
        return (points[0][0], points[0][1], 0.0)

    shuffled = list(points)
    random.Random(0xC1C1).shuffle(shuffled)

    circle: Optional[tuple[float, float, float]] = None
    for i, p in enumerate(shuffled):
        if circle is None or not _sec_in_circle(circle, p):
            circle = _sec_one_point(shuffled[: i + 1], p)
    assert circle is not None
    return circle


def _sec_one_point(points: list[tuple[float, float]], q: tuple[float, float]) -> tuple[float, float, float]:
    circle = (q[0], q[1], 0.0)
    for i, p in enumerate(points):
        if not _sec_in_circle(circle, p):
            if circle[2] == 0.0:
                circle = _sec_diameter(q, p)
            else:
                circle = _sec_two_points(points[: i + 1], q, p)
    return circle


def _sec_two_points(
    points: list[tuple[float, float]],
    q: tuple[float, float],
    r: tuple[float, float],
) -> tuple[float, float, float]:
    circle = _sec_diameter(q, r)
    left: Optional[tuple[float, float, float]] = None
    right: Optional[tuple[float, float, float]] = None
    px, py = q
    qx, qy = r
    for p in points:
        if _sec_in_circle(circle, p):
            continue
        cross = _sec_cross(px, py, qx, qy, p[0], p[1])
        circumcircle = _sec_circumcircle(q, r, p)
        if circumcircle is None:
            continue
        ccross = _sec_cross(px, py, qx, qy, circumcircle[0], circumcircle[1])
        if cross > 0.0 and (
            left is None
            or ccross > _sec_cross(px, py, qx, qy, left[0], left[1])
        ):
            left = circumcircle
        elif cross < 0.0 and (
            right is None
            or ccross < _sec_cross(px, py, qx, qy, right[0], right[1])
        ):
            right = circumcircle
    if left is None and right is None:
        return circle
    if left is None:
        return right  # type: ignore[return-value]
    if right is None:
        return left
    return left if left[2] <= right[2] else right


def _sec_diameter(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float, float]:
    cx = (a[0] + b[0]) / 2.0
    cy = (a[1] + b[1]) / 2.0
    r = max(_sec_distance(cx, cy, a[0], a[1]), _sec_distance(cx, cy, b[0], b[1]))
    return (cx, cy, r)


def _sec_circumcircle(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> Optional[tuple[float, float, float]]:
    ox = (min(a[0], b[0], c[0]) + max(a[0], b[0], c[0])) / 2.0
    oy = (min(a[1], b[1], c[1]) + max(a[1], b[1], c[1])) / 2.0
    ax = a[0] - ox
    ay = a[1] - oy
    bx = b[0] - ox
    by = b[1] - oy
    cx = c[0] - ox
    cy = c[1] - oy
    d = (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by)) * 2.0
    if d == 0.0:
        return None
    x = ox + (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / d
    y = oy + (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / d
    ra = _sec_distance(x, y, a[0], a[1])
    rb = _sec_distance(x, y, b[0], b[1])
    rc = _sec_distance(x, y, c[0], c[1])
    return (x, y, max(ra, rb, rc))


def _sec_in_circle(circle: tuple[float, float, float], p: tuple[float, float]) -> bool:
    return _sec_distance(circle[0], circle[1], p[0], p[1]) <= circle[2] * _SEC_EPS_FACTOR


def _sec_distance(x1: float, y1: float, x2: float, y2: float) -> float:
    dx = x1 - x2
    dy = y1 - y2
    return sqrt(dx * dx + dy * dy)


def _sec_cross(ox: float, oy: float, ax: float, ay: float, bx: float, by: float) -> float:
    return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)


def _point_in_proximity_zone(zone: Zone, latitude: float, longitude: float) -> bool:
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    contract_type = str(params.get("contractType") or "").strip().lower()
    if contract_type != "proximity":
        return False

    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    circles = _extract_proximity_circle_specs(geometry, config)
    if not circles:
        return False

    for circle in circles:
        center = circle.get("center")
        radius = circle.get("radius_meters")
        if not isinstance(center, dict):
            continue
        c_lat = center.get("latitude")
        c_lng = center.get("longitude")
        if not isinstance(c_lat, (int, float)) or not isinstance(c_lng, (int, float)):
            continue
        if not isinstance(radius, (int, float)) or radius <= 0:
            continue
        distance = _haversine_meters(latitude, longitude, float(c_lat), float(c_lng))
        if distance <= radius:
            return True
    return False


def _point_in_object_zone(
    db: Session,
    zone: Zone,
    latitude: float,
    longitude: float,
    owner_ids: list[int],
) -> bool:
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    contract_type = str(params.get("contractType") or "").strip().lower()
    if contract_type != "object":
        return False

    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    object_center = _resolve_object_center(geometry)
    radius = config.get("radius_meters")
    if not isinstance(object_center, dict):
        return False
    c_lat = object_center.get("latitude")
    c_lng = object_center.get("longitude")
    if not isinstance(c_lat, (int, float)) or not isinstance(c_lng, (int, float)):
        return False
    if not isinstance(radius, (int, float)) or radius <= 0:
        return False
    distance = _haversine_meters(latitude, longitude, float(c_lat), float(c_lng))
    return distance <= radius


def _resolve_object_center(geometry: dict[str, Any]) -> dict[str, Any] | None:
    center = geometry.get("center")
    return center if isinstance(center, dict) else None


def _extract_proximity_circle_specs(geometry: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    circles = geometry.get("circles")
    if isinstance(circles, list):
        normalized: list[dict[str, Any]] = []
        for circle in circles:
            if isinstance(circle, dict):
                normalized.append(
                    {
                        "center": circle.get("center"),
                        "radius_meters": circle.get("radius_meters"),
                    }
                )
        if normalized:
            return normalized

    centers = geometry.get("centers")
    radii = config.get("radii_meters")
    if isinstance(centers, list) and isinstance(radii, list):
        pairs: list[dict[str, Any]] = []
        for center, radius in zip(centers, radii):
            if isinstance(center, dict):
                pairs.append({"center": center, "radius_meters": radius})
        if pairs:
            return pairs

    center = geometry.get("center")
    radius = config.get("radius_meters")
    if isinstance(center, dict) and isinstance(radius, (int, float)):
        return [{"center": center, "radius_meters": radius}]

    return []


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in meters."""
    earth_radius_m = 6_371_000.0
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return earth_radius_m * c
