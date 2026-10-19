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


LIMIT_ACCOUNT_TYPES = ("private_plus", "exclusive", "enhanced", "enhanced_plus")
LIMIT_ROLES = ("administrator", "user")
LIMIT_UNITS = ("m2", "km2")


def _ensure_rules_column(db: Session) -> None:
    from sqlalchemy import inspect, text

    bind = db.get_bind()
    try:
        columns = {col["name"] for col in inspect(bind).get_columns("zone_size_limits")}
    except Exception:
        return
    if "rules" in columns:
        return
    db.execute(text("ALTER TABLE zone_size_limits ADD COLUMN rules JSON"))
    db.commit()


def get_limits(db: Session) -> ZoneSizeLimit:
    _ensure_rules_column(db)
    row = db.get(ZoneSizeLimit, 1)
    if row is None:
        row = ZoneSizeLimit(id=1, each_zone_m2=None, total_m2=None, rules=[])
        db.add(row)
        db.flush()
    return row


def _clean_rule(raw: dict[str, Any]) -> dict[str, Any]:
    account_type = str(raw.get("account_type") or "").strip().lower()
    role = str(raw.get("role") or "").strip().lower()
    if account_type not in LIMIT_ACCOUNT_TYPES or role not in LIMIT_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Each limit needs a known account type and a network admin or member role.",
        )
    each_unit = str(raw.get("each_unit") or "km2").strip().lower()
    total_unit = str(raw.get("total_unit") or "km2").strip().lower()
    if each_unit not in LIMIT_UNITS or total_unit not in LIMIT_UNITS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unit must be square meters or square kilometers.",
        )

    def _amount(key: str, label: str) -> float | None:
        value = raw.get(key)
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = -1
        if number <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{label} must be greater than zero, or empty for no limit.",
            )
        return number

    return {
        "account_type": account_type,
        "role": role,
        "each_zone_m2": _amount("each_zone_m2", "Each-zone limit"),
        "each_unit": each_unit,
        "total_m2": _amount("total_m2", "Total limit"),
        "total_unit": total_unit,
    }


def list_rules(db: Session) -> list[dict[str, Any]]:
    row = get_limits(db)
    stored = row.rules if isinstance(row.rules, list) else []
    by_key = {
        (str(item.get("account_type")), str(item.get("role"))): item
        for item in stored
        if isinstance(item, dict)
    }
    rules: list[dict[str, Any]] = []
    for account_type in LIMIT_ACCOUNT_TYPES:
        for role in LIMIT_ROLES:
            found = by_key.get((account_type, role), {})
            rules.append(
                {
                    "account_type": account_type,
                    "role": role,
                    "each_zone_m2": found.get("each_zone_m2"),
                    "each_unit": found.get("each_unit") or "km2",
                    "total_m2": found.get("total_m2"),
                    "total_unit": found.get("total_unit") or "km2",
                }
            )
    return rules


def set_limits(db: Session, caller: Owner, *, rules: list[dict[str, Any]]) -> dict[str, Any]:
    if not is_system_administrator(caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the system administrator can set zone size limits.",
        )
    cleaned = [_clean_rule(item) for item in rules]
    row = get_limits(db)
    row.rules = cleaned
    db.commit()
    db.refresh(row)
    return {"rules": list_rules(db)}


def _rule_for_owner(db: Session, owner: Owner) -> dict[str, Any]:
    account_type = str(owner.account_type.value or "").strip().lower()
    role = "administrator" if (owner.role.value or "").strip().lower() == "administrator" else "user"
    for rule in list_rules(db):
        if rule["account_type"] == account_type and rule["role"] == role:
            return rule
    return {"each_zone_m2": None, "total_m2": None}


def account_used_m2(db: Session, owner: Owner, *, exclude_zone_id: int | None = None) -> float:
    """Area of the zones this person created. Their role's total cap applies to that sum."""
    rows = (
        db.query(Zone)
        .filter(Zone.creator_id == owner.id, Zone.active.is_(True))
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
    limits = _rule_for_owner(db, owner)
    if limits.get("each_zone_m2") is None and limits.get("total_m2") is None:
        return
    draft = measure_area_m2(geometry, config, h3_cells)
    used = account_used_m2(db, owner, exclude_zone_id=exclude_zone_id)
    reason = area_block_reason(
        draft,
        used,
        each_zone_m2=limits.get("each_zone_m2"),
        total_m2=limits.get("total_m2"),
    )
    if reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=reason,
        )


def budget_for(db: Session, owner: Owner, *, exclude_zone_id: int | None = None) -> dict[str, float | None]:
    limits = _rule_for_owner(db, owner)
    each = limits.get("each_zone_m2")
    total = limits.get("total_m2")
    used = account_used_m2(db, owner, exclude_zone_id=exclude_zone_id)
    remaining = None if total is None else max(0.0, float(total) - used)
    return {
        "each_zone_m2": each,
        "total_m2": total,
        "used_m2": used,
        "remaining_total_m2": remaining,
    }
