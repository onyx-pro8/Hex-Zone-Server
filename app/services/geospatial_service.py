"""Geospatial evaluation using H3, dynamic clusters, and PostGIS-compatible data."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from math import atan2, cos, radians, sin, sqrt
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.h3_utils import lat_lng_to_h3_cell
from app.models import Owner, Zone


def point_h3_cells(latitude: float, longitude: float) -> list[str]:
    """All H3 cell ids that contain ``(latitude, longitude)``, one per resolution.

    Zones store H3 cells at whatever resolution the builder used (the dashboard
    defaults to 6, the zone builder to 9, etc.). A point lies inside a stored
    cell of resolution ``R`` exactly when the point's own cell at resolution
    ``R`` equals that stored cell. Enumerating the point's cell at every
    supported resolution lets containment work regardless of the resolution the
    zone was saved at, instead of assuming a single fixed resolution.
    """
    cells: list[str] = []
    for resolution in range(settings.H3_MIN_RESOLUTION, settings.H3_MAX_RESOLUTION + 1):
        try:
            cells.append(lat_lng_to_h3_cell(latitude, longitude, resolution))
        except Exception:  # noqa: BLE001 - skip resolutions h3 rejects
            continue
    return cells


def _dialect_is_postgresql(db: Session) -> bool:
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:  # noqa: BLE001 - be permissive if bind can't be resolved
        return False


def _geojson_polygon_from_zone(zone: Zone) -> dict | None:
    """Polygon geometry from zone parameters when the PostGIS column was never set.

    Contract ``POST /zones`` historically stored polygons only under
    ``parameters.geometry.geo_fence_polygon``, leaving ``zones.geo_fence_polygon``
    NULL so ``ST_Contains`` queries never matched.
    """
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    nested = geometry.get("geo_fence_polygon")
    if isinstance(nested, dict) and nested.get("type") in ("Polygon", "MultiPolygon"):
        return nested
    if geometry.get("type") in ("Polygon", "MultiPolygon"):
        return geometry
    return None


def _point_in_geojson_ring(lat: float, lng: float, ring: list[list[float]]) -> bool:
    """Ray-casting point-in-ring for GeoJSON rings ``[[lng, lat], ...]``."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        intersects = (yi > lat) != (yj > lat) and lng < (
            (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _point_in_geojson_polygon_python(
    geojson: dict,
    latitude: float,
    longitude: float,
) -> bool:
    """Fallback containment when PostGIS is unavailable (tests / SQLite)."""
    gtype = geojson.get("type")
    polys: list[list[list[list[float]]]] = []
    if gtype == "Polygon":
        polys = [geojson["coordinates"]]
    elif gtype == "MultiPolygon":
        polys = geojson["coordinates"]
    else:
        return False

    for polygon in polys:
        if not polygon:
            continue
        outer = polygon[0]
        if not _point_in_geojson_ring(latitude, longitude, outer):
            continue
        in_hole = False
        for hole in polygon[1:]:
            if _point_in_geojson_ring(latitude, longitude, hole):
                in_hole = True
                break
        if not in_hole:
            return True
    return False


def _postgis_point_in_geojson_polygon(
    db: Session,
    geojson: dict,
    latitude: float,
    longitude: float,
) -> bool:
    """Test whether ``(latitude, longitude)`` lies inside a GeoJSON polygon."""
    if not _dialect_is_postgresql(db):
        return _point_in_geojson_polygon_python(geojson, latitude, longitude)

    sql = text(
        """
        SELECT
            ST_Covers(
                ST_SetSRID(ST_GeomFromGeoJSON(CAST(:geojson AS json)), 4326),
                ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)
            )
            OR ST_Covers(
                ST_SetSRID(ST_GeomFromGeoJSON(CAST(:geojson AS json)), 4326),
                ST_SetSRID(ST_MakePoint(:longitude + 360, :latitude), 4326)
            )
            OR ST_Covers(
                ST_SetSRID(ST_GeomFromGeoJSON(CAST(:geojson AS json)), 4326),
                ST_SetSRID(ST_MakePoint(:longitude - 360, :latitude), 4326)
            )
        """
    )
    row = db.execute(
        sql,
        {
            "geojson": json.dumps(geojson),
            "latitude": latitude,
            "longitude": longitude,
        },
    ).first()
    return bool(row and row[0])


def _point_in_zone_polygon(
    db: Session,
    zone: Zone,
    latitude: float,
    longitude: float,
) -> bool:
    """Containment for a zone polygon stored in PostGIS or parameters JSON."""
    geojson = _geojson_polygon_from_zone(zone)
    if geojson is not None:
        return _postgis_point_in_geojson_polygon(db, geojson, latitude, longitude)
    return False

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

    point_cells = point_h3_cells(latitude, longitude)
    matched: set[str] = set()
    if point_cells:
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
                  WHERE cell = ANY(:point_cells)
              )
            """
        )
        matched = {
            row[0]
            for row in db.execute(
                h3_sql,
                {"owner_ids": owner_ids, "point_cells": point_cells},
            )
        }

    # Test the point in each world copy (longitude, ±360). ST_Contains is planar,
    # so a polygon stored at wrapped longitudes (drawn across repeated world
    # copies in the map UI) only matches when the point is shifted into the same
    # copy. Checking all three bands matches such polygons without re-saving them.
    postgis_sql = text(
        """
        SELECT z.zone_id
        FROM zones z
        WHERE z.owner_id = ANY(:owner_ids)
          AND z.active = TRUE
          AND z.geo_fence_polygon IS NOT NULL
          AND (
              ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326))
              OR ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(:longitude + 360, :latitude), 4326))
              OR ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(:longitude - 360, :latitude), 4326))
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
        if zone.zone_id in matched:
            continue
        if _geojson_polygon_from_zone(zone) is not None:
            if _point_in_zone_polygon(db, zone, latitude, longitude):
                matched.add(zone.zone_id)
                continue
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
    """Return zone_id labels for zone rows whose geometry contains the point.

    Prefer :func:`evaluate_zone_records_containing_point` for message propagation
    so recipient fan-out keys off the exact matched geometries, not every row that
    shares the same ``zone_id`` string.
    """
    record_ids = evaluate_zone_records_containing_point(db, latitude, longitude)
    return zone_ids_for_zone_records(db, record_ids)


def _zone_record_ids_containing_point(
    db: Session,
    latitude: float,
    longitude: float,
    candidate_owner_ids: Iterable[int],
    *,
    include_dynamic_zones: bool = True,
) -> list[int]:
    """Primary keys of active zone rows whose geometry contains the point."""
    owner_ids = list(candidate_owner_ids)
    if not owner_ids:
        return []

    point_cells = point_h3_cells(latitude, longitude)
    matched: set[int] = set()
    if point_cells:
        h3_sql = text(
            """
            SELECT z.id
            FROM zones z
            WHERE z.owner_id = ANY(:owner_ids)
              AND z.active = TRUE
              AND z.h3_cells IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM json_array_elements_text(z.h3_cells) AS cell
                  WHERE cell = ANY(:point_cells)
              )
            """
        )
        for row in db.execute(
            h3_sql,
            {"owner_ids": owner_ids, "point_cells": point_cells},
        ):
            matched.add(int(row[0]))

    postgis_sql = text(
        """
        SELECT z.id
        FROM zones z
        WHERE z.owner_id = ANY(:owner_ids)
          AND z.active = TRUE
          AND z.geo_fence_polygon IS NOT NULL
          AND (
              ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326))
              OR ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(:longitude + 360, :latitude), 4326))
              OR ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(:longitude - 360, :latitude), 4326))
          )
        """
    )
    for row in db.execute(
        postgis_sql,
        {"owner_ids": owner_ids, "longitude": longitude, "latitude": latitude},
    ):
        matched.add(int(row[0]))

    circle_candidates = (
        db.query(Zone)
        .filter(Zone.owner_id.in_(owner_ids), Zone.active.is_(True))
        .all()
    )
    for zone in circle_candidates:
        if zone.id in matched:
            continue
        if _geojson_polygon_from_zone(zone) is not None:
            if _point_in_zone_polygon(db, zone, latitude, longitude):
                matched.add(int(zone.id))
                continue
        if include_dynamic_zones and _point_in_dynamic_zone(zone, latitude, longitude):
            matched.add(int(zone.id))
            continue
        if _point_in_proximity_zone(zone, latitude, longitude):
            matched.add(int(zone.id))
            continue
        if _point_in_object_zone(db, zone, latitude, longitude, owner_ids):
            matched.add(int(zone.id))

    return sorted(matched)


def evaluate_zone_records_containing_point(
    db: Session,
    latitude: float,
    longitude: float,
) -> list[int]:
    """Zone row ids (``zones.id``) whose geometry contains the message point."""
    active_owner_rows = db.query(Owner.id).filter(Owner.active.is_(True)).all()
    owner_ids = [int(row[0]) for row in active_owner_rows]
    return _zone_record_ids_containing_point(
        db,
        latitude,
        longitude,
        owner_ids,
        include_dynamic_zones=True,
    )


def zone_ids_for_zone_records(db: Session, zone_record_ids: Iterable[int]) -> list[str]:
    """Distinct ``zone_id`` labels for the given zone rows (metadata / display)."""
    target = [int(z) for z in zone_record_ids if z]
    if not target:
        return []
    rows = (
        db.query(Zone.zone_id)
        .filter(Zone.id.in_(target), Zone.active.is_(True))
        .distinct()
        .all()
    )
    return sorted({str(row[0]) for row in rows if row[0]})


def _point_in_zone_row(
    db: Session,
    zone: Zone,
    latitude: float,
    longitude: float,
) -> bool:
    cells = zone.h3_cells
    if isinstance(cells, (list, tuple)) and cells:
        point_cells = set(point_h3_cells(latitude, longitude))
        if point_cells & {str(c) for c in cells}:
            return True
    if _geojson_polygon_from_zone(zone) is not None:
        return _point_in_zone_polygon(db, zone, latitude, longitude)
    if _point_in_dynamic_zone(zone, latitude, longitude):
        return True
    if _point_in_proximity_zone(zone, latitude, longitude):
        return True
    return _point_in_object_zone(db, zone, latitude, longitude, [])


def owner_ids_located_within_zone_records(
    db: Session,
    zone_record_ids: Iterable[int],
    *,
    exclude_owner_id: int | None = None,
) -> list[int]:
    """Owners whose stored location lies inside one of the given zone rows.

    Unlike :func:`owner_ids_located_within_zone_ids`, this only tests the exact
    zone geometries that matched the sender — not every row sharing a ``zone_id``.
    """
    target = list(dict.fromkeys(int(z) for z in zone_record_ids if z))
    if not target:
        return []

    matched: set[int] = set()

    if _dialect_is_postgresql(db):
        poly_sql = text(
            """
            SELECT DISTINCT o.id
            FROM owners o
            JOIN zones z
              ON z.id = ANY(:zone_record_ids)
             AND z.active = TRUE
             AND z.geo_fence_polygon IS NOT NULL
            WHERE o.active = TRUE
              AND o.latitude IS NOT NULL
              AND o.longitude IS NOT NULL
              AND (
                  ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(o.longitude, o.latitude), 4326))
                  OR ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(o.longitude + 360, o.latitude), 4326))
                  OR ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(o.longitude - 360, o.latitude), 4326))
              )
            """
        )
        for row in db.execute(poly_sql, {"zone_record_ids": target}):
            matched.add(int(row[0]))

    zone_rows = (
        db.query(Zone)
        .filter(Zone.id.in_(target), Zone.active.is_(True))
        .all()
    )
    if not zone_rows:
        if exclude_owner_id is not None:
            matched.discard(int(exclude_owner_id))
        return sorted(matched)

    owner_rows = (
        db.query(Owner.id, Owner.latitude, Owner.longitude)
        .filter(
            Owner.active.is_(True),
            Owner.latitude.isnot(None),
            Owner.longitude.isnot(None),
        )
        .all()
    )
    for owner_id, lat, lng in owner_rows:
        oid = int(owner_id)
        if oid in matched:
            continue
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            continue
        latf = float(lat)
        lngf = float(lng)
        for zone in zone_rows:
            if _point_in_zone_row(db, zone, latf, lngf):
                matched.add(oid)
                break

    if exclude_owner_id is not None:
        matched.discard(int(exclude_owner_id))
    return sorted(matched)


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


def owner_ids_located_within_zone_ids(
    db: Session,
    zone_ids: Iterable[str],
    *,
    exclude_owner_id: int | None = None,
) -> list[int]:
    """Active owners whose **current stored location** falls inside any of ``zone_ids``.

    This implements realtime presence: a recipient is "inside the zone" when
    their own ``owners.latitude / longitude`` is contained by the geometry of an
    active zone whose ``zone_id`` is in ``zone_ids`` (regardless of which owner
    defined that zone). Polygon zones are evaluated in a single batched PostGIS
    query; H3 and circle-based zone types are evaluated in Python.
    """
    target = list(dict.fromkeys(str(z) for z in zone_ids if z))
    if not target:
        return []

    matched: set[int] = set()

    if _dialect_is_postgresql(db):
        poly_sql = text(
            """
            SELECT DISTINCT o.id
            FROM owners o
            JOIN zones z
              ON z.zone_id = ANY(:zone_ids)
             AND z.active = TRUE
             AND z.geo_fence_polygon IS NOT NULL
            WHERE o.active = TRUE
              AND o.latitude IS NOT NULL
              AND o.longitude IS NOT NULL
              AND (
                  ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(o.longitude, o.latitude), 4326))
                  OR ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(o.longitude + 360, o.latitude), 4326))
                  OR ST_Covers(z.geo_fence_polygon, ST_SetSRID(ST_MakePoint(o.longitude - 360, o.latitude), 4326))
              )
            """
        )
        for row in db.execute(poly_sql, {"zone_ids": target}):
            matched.add(int(row[0]))

    zone_rows = (
        db.query(Zone)
        .filter(Zone.zone_id.in_(target), Zone.active.is_(True))
        .all()
    )
    h3_cell_sets: list[set[str]] = []
    for zone in zone_rows:
        cells = zone.h3_cells
        if isinstance(cells, (list, tuple)) and cells:
            h3_cell_sets.append({str(c) for c in cells})

    if zone_rows:
        owner_rows = (
            db.query(Owner.id, Owner.latitude, Owner.longitude)
            .filter(
                Owner.active.is_(True),
                Owner.latitude.isnot(None),
                Owner.longitude.isnot(None),
            )
            .all()
        )
        for owner_id, lat, lng in owner_rows:
            oid = int(owner_id)
            if oid in matched:
                continue
            if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
                continue
            latf = float(lat)
            lngf = float(lng)

            if h3_cell_sets:
                point_cells = set(point_h3_cells(latf, lngf))
                if any(point_cells & cells for cells in h3_cell_sets):
                    matched.add(oid)
                    continue

            for zone in zone_rows:
                if _geojson_polygon_from_zone(zone) is not None:
                    if _point_in_zone_polygon(db, zone, latf, lngf):
                        matched.add(oid)
                        break
                if (
                    _point_in_dynamic_zone(zone, latf, lngf)
                    or _point_in_proximity_zone(zone, latf, lngf)
                    or _point_in_object_zone(db, zone, latf, lngf, [])
                ):
                    matched.add(oid)
                    break

    if exclude_owner_id is not None:
        matched.discard(int(exclude_owner_id))
    return sorted(matched)


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
