"""Area of every zone shape, and the two system-admin caps."""
from __future__ import annotations

import math
from typing import Any

import h3
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.owner import Owner
from app.models.zone import Zone
from app.models.zone_size_limit import ZoneSizeLimit
from app.services.account_type_policy import is_system_administrator
from app.services.zone_policy import account_owner_ids_for_policy

_EARTH_RADIUS_M = 6_378_137.0


def ring_area_m2(ring: list) -> float:
    """Spherical area of a ring of [lng, lat] points."""
    points: list[tuple[float, float]] = []
    for point in ring:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            points.append((float(point[0]), float(point[1])))
    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index, (lng, lat) in enumerate(points):
        lng2, lat2 = points[(index + 1) % len(points)]
        total += math.radians(lng2 - lng) * (
            2 + math.sin(math.radians(lat)) + math.sin(math.radians(lat2))
        )
    return abs(total * _EARTH_RADIUS_M * _EARTH_RADIUS_M / 2.0)


def _polygon_area(polygon: dict[str, Any] | None) -> float:
    if not isinstance(polygon, dict):
        return 0.0
    kind = polygon.get("type")
    coords = polygon.get("coordinates")
    if kind == "Polygon" and isinstance(coords, list) and coords:
        area = ring_area_m2(coords[0])
        for hole in coords[1:]:
            area -= ring_area_m2(hole)
        return max(0.0, area)
    if kind == "MultiPolygon" and isinstance(coords, list):
        return sum(_polygon_area({"type": "Polygon", "coordinates": part}) for part in coords)
    return 0.0


def _h3_area(cells: object) -> float:
    if not isinstance(cells, list):
        return 0.0
    total = 0.0
    seen: set[str] = set()
    for cell in cells:
        token = str(cell or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        try:
            total += float(h3.cell_area(token, unit="m^2"))
        except Exception:
            continue
    return total


def _radius_area(config: dict[str, Any]) -> float:
    for key in ("resolved_radius_meters", "radius_meters"):
        try:
            radius = float(config.get(key) or 0)
        except (TypeError, ValueError):
            radius = 0.0
        if radius > 0:
            return math.pi * radius * radius
    return 0.0


def measure_area_m2(
    geometry: dict[str, Any] | None,
    config: dict[str, Any] | None,
    h3_cells: list | None = None,
) -> float:
    """One zone's area. A polygon wins, then hex cells, then a circle radius."""
    geometry = geometry if isinstance(geometry, dict) else {}
    config = config if isinstance(config, dict) else {}
    polygon = geometry.get("geo_fence_polygon")
    if not isinstance(polygon, dict):
        polygon = geometry.get("geoFencePolygon") if isinstance(geometry.get("geoFencePolygon"), dict) else None
    polygon_area = _polygon_area(polygon if isinstance(polygon, dict) else None)
    if polygon_area > 0:
        return polygon_area
    cells = h3_cells if h3_cells else config.get("h3_cells") or config.get("h3Cells") or []
    cell_area = _h3_area(cells)
    if cell_area > 0:
        return cell_area
    return _radius_area(config)


def zone_area_m2(zone: Zone) -> float:
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    if not geometry.get("geo_fence_polygon") and isinstance(zone.geo_fence_polygon, dict):
        geometry = {**geometry, "geo_fence_polygon": zone.geo_fence_polygon}
    return measure_area_m2(geometry, config, list(zone.h3_cells or []))


def get_limits(db: Session) -> ZoneSizeLimit:
    row = db.get(ZoneSizeLimit, 1)
    if row is None:
        row = ZoneSizeLimit(id=1, each_zone_m2=None, total_m2=None)
        db.add(row)
        db.flush()
    return row


def limits_payload(row: ZoneSizeLimit) -> dict[str, float | None]:
    return {
        "each_zone_m2": row.each_zone_m2,
        "total_m2": row.total_m2,
    }


def set_limits(
    db: Session,
    caller: Owner,
    *,
    each_zone_m2: float | None,
    total_m2: float | None,
) -> dict[str, float | None]:
    if not is_system_administrator(caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the system administrator can set zone size limits.",
        )
    for label, value in (("Each-zone limit", each_zone_m2), ("Total limit", total_m2)):
        if value is not None and value <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{label} must be greater than zero, or empty for no limit.",
            )
    row = get_limits(db)
    row.each_zone_m2 = each_zone_m2
    row.total_m2 = total_m2
    db.commit()
    db.refresh(row)
    return limits_payload(row)


def account_used_m2(db: Session, owner: Owner, *, exclude_zone_id: int | None = None) -> float:
    owner_ids = account_owner_ids_for_policy(db, owner)
    if not owner_ids:
        return 0.0
    rows = (
        db.query(Zone)
        .filter(Zone.owner_id.in_(tuple(owner_ids)), Zone.active.is_(True))
        .all()
    )
    total = 0.0
    for zone in rows:
        if exclude_zone_id is not None and int(zone.id) == int(exclude_zone_id):
            continue
        total += zone_area_m2(zone)
    return total


def area_block_reason(
    draft_m2: float,
    used_m2: float,
    *,
    each_zone_m2: float | None,
    total_m2: float | None,
) -> str | None:
    if each_zone_m2 is not None and draft_m2 > each_zone_m2 + 1:
        return "This zone is bigger than the each-zone limit."
    if total_m2 is not None and used_m2 + draft_m2 > total_m2 + 1:
        return "Not enough total size left."
    return None


def enforce_zone_area(
    db: Session,
    owner: Owner,
    *,
    geometry: dict[str, Any] | None,
    config: dict[str, Any] | None,
    h3_cells: list | None = None,
    exclude_zone_id: int | None = None,
) -> None:
    limits = get_limits(db)
    if limits.each_zone_m2 is None and limits.total_m2 is None:
        return
    draft = measure_area_m2(geometry, config, h3_cells)
    used = account_used_m2(db, owner, exclude_zone_id=exclude_zone_id)
    reason = area_block_reason(
        draft,
        used,
        each_zone_m2=limits.each_zone_m2,
        total_m2=limits.total_m2,
    )
    if reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=reason,
        )


def budget_for(db: Session, owner: Owner, *, exclude_zone_id: int | None = None) -> dict[str, float | None]:
    limits = get_limits(db)
    used = account_used_m2(db, owner, exclude_zone_id=exclude_zone_id)
    remaining = None if limits.total_m2 is None else max(0.0, float(limits.total_m2) - used)
    return {
        "each_zone_m2": limits.each_zone_m2,
        "total_m2": limits.total_m2,
        "used_m2": used,
        "remaining_total_m2": remaining,
    }
