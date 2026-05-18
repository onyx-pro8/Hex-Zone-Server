"""Geospatial evaluation using H3, dynamic circles, and PostGIS-compatible data."""
from math import atan2, cos, radians, sin, sqrt
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.h3_utils import lat_lng_to_h3_cell
from app.models import Zone


def evaluate_member_zones(db: Session, latitude: float, longitude: float, candidate_owner_ids: Iterable[int]) -> list[str]:
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
        if _point_in_dynamic_zone(zone, latitude, longitude):
            matched.add(zone.zone_id)
            continue
        if _point_in_proximity_zone(zone, latitude, longitude):
            matched.add(zone.zone_id)
            continue
        if _point_in_object_zone(db, zone, latitude, longitude, owner_ids):
            matched.add(zone.zone_id)

    return sorted(matched)


def _point_in_dynamic_zone(zone: Zone, latitude: float, longitude: float) -> bool:
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    contract_type = str(params.get("contractType") or "").strip().lower()
    if contract_type != "dynamic":
        return False

    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    circle_specs = _extract_dynamic_circle_specs(geometry, config)
    if not circle_specs:
        return False

    for circle in circle_specs:
        center = circle.get("center")
        min_radius = circle.get("min_radius_meters")
        max_radius = circle.get("max_radius_meters")
        if not isinstance(center, dict):
            continue
        c_lat = center.get("latitude")
        c_lng = center.get("longitude")
        if not isinstance(c_lat, (int, float)) or not isinstance(c_lng, (int, float)):
            continue
        if not isinstance(min_radius, (int, float)) or not isinstance(max_radius, (int, float)):
            continue
        if min_radius < 0 or max_radius < min_radius:
            continue

        distance = _haversine_meters(latitude, longitude, float(c_lat), float(c_lng))
        if min_radius <= distance <= max_radius:
            return True
    return False


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


def _extract_dynamic_circle_specs(geometry: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    circles = geometry.get("circles")
    if isinstance(circles, list):
        normalized = [circle for circle in circles if isinstance(circle, dict)]
        if normalized:
            return normalized

    centers = geometry.get("centers")
    circle_ranges = config.get("circle_ranges")
    if isinstance(centers, list) and isinstance(circle_ranges, list):
        pairs: list[dict[str, Any]] = []
        for center, radius_range in zip(centers, circle_ranges):
            if isinstance(center, dict) and isinstance(radius_range, dict):
                pairs.append(
                    {
                        "center": center,
                        "min_radius_meters": radius_range.get("min_radius_meters"),
                        "max_radius_meters": radius_range.get("max_radius_meters"),
                    }
                )
        if pairs:
            return pairs

    center = geometry.get("center")
    min_radius = config.get("min_radius_meters")
    max_radius = config.get("max_radius_meters")
    if isinstance(center, dict) and isinstance(min_radius, (int, float)) and isinstance(max_radius, (int, float)):
        return [
            {
                "center": center,
                "min_radius_meters": min_radius,
                "max_radius_meters": max_radius,
            }
        ]
    return []


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
