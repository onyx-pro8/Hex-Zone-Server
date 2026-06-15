"""CRUD operations for Zone."""
import json
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.future import select
from sqlalchemy import func
from app.models import Zone
from app.models.zone import ZoneType, _normalize_latitude, _normalize_longitude
from app.schemas.schemas import ZoneCreate, ZoneUpdate
from app.core.h3_utils import has_h3_overlap, lat_lng_to_h3_cell, validate_h3_cell
from typing import Optional, List, Any, Sequence


def _polygon_coords_to_wkt(polygon_coords: list[list[list[float]]]) -> str:
    rings = []
    for ring in polygon_coords:
        rings.append(
            "("
            + ", ".join(
                f"{_normalize_longitude(lng)} {_normalize_latitude(lat)}"
                for lng, lat in ring
            )
            + ")"
        )
    return "(" + ",".join(rings) + ")"


def geojson_to_wkt(geojson: dict) -> str:
    geometry_type = geojson.get("type")
    if geometry_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("geo_fence_polygon must be a GeoJSON Polygon or MultiPolygon")

    if geometry_type == "Polygon":
        polygon_text = _polygon_coords_to_wkt(geojson["coordinates"])
        return f"MULTIPOLYGON({polygon_text})"

    multipolygon_text = ",".join(_polygon_coords_to_wkt(polygon) for polygon in geojson["coordinates"])
    return f"MULTIPOLYGON({multipolygon_text})"


def _geojson_to_geometry(geojson: Optional[dict]):
    if geojson is None:
        return None
    wkt = geojson_to_wkt(geojson)
    return f"SRID=4326;{wkt}"


def _sync_parameters_h3_cells(db_zone: Zone) -> None:
    """Keep parameters.config.h3Cells aligned with zone.h3_cells."""
    params = db_zone.parameters if isinstance(db_zone.parameters, dict) else {}
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    config["h3Cells"] = list(db_zone.h3_cells or [])
    params["config"] = config
    db_zone.parameters = params


def create_zone(db: Session, owner_id: int, creator_id: int, zone: ZoneCreate) -> Zone:
    """Create a new zone."""
    h3_cells = zone.h3_cells.copy()
    geo_fence_polygon = None

    # If latitude and longitude provided, add the center cell
    if zone.latitude is not None and zone.longitude is not None:
        resolution = zone.h3_resolution or 13
        center_cell = lat_lng_to_h3_cell(zone.latitude, zone.longitude, resolution)
        if center_cell not in h3_cells:
            h3_cells.append(center_cell)

    if zone.geo_fence_polygon is not None:
        geo_fence_polygon = _geojson_to_geometry(zone.geo_fence_polygon)

    if h3_cells:
        if any(not validate_h3_cell(cell) for cell in h3_cells):
            raise ValueError("Invalid H3 cell id")
        if has_h3_overlap(h3_cells):
            raise ValueError("Overlapping H3 cells are not allowed across resolutions")

    db_zone = Zone(
        zone_id=zone.zone_id,
        owner_id=owner_id,
        creator_id=creator_id,
        zone_type=ZoneType(zone.zone_type),
        name=zone.name,
        description=zone.description,
        h3_cells=h3_cells,
        geo_fence_polygon=geo_fence_polygon,
        parameters=zone.parameters or {},
    )
    _sync_parameters_h3_cells(db_zone)
    db.add(db_zone)
    db.flush()
    db.refresh(db_zone)
    return db_zone


def _geojson_text_to_dict(geojson_text: Optional[str]) -> Optional[dict]:
    if not geojson_text:
        return None
    return json.loads(geojson_text)


def apply_zone_geo_fence_geojson(zone: Zone, geojson_text: Optional[str]) -> None:
    """Set geo_fence_polygon to a GeoJSON dict for API responses.

    ST_AsGeoJSON is applied in the query; we must not assign via the attribute
    setter because Zone.validate_geo_fence_polygon converts dicts to EWKT.
    """
    set_committed_value(zone, "geo_fence_polygon", _geojson_text_to_dict(geojson_text))


def get_zone(db: Session, zone_id: Optional[str] = None, owner_id: Optional[int] = None) -> Optional[Zone]:
    """Get a zone by zone_id and/or owner_id."""
    query = select(Zone)
    if zone_id is not None:
        query = query.where(Zone.zone_id == zone_id)
    if owner_id is not None:
        query = query.where(Zone.owner_id == owner_id)
    result = db.execute(query)
    return result.scalars().first()


def get_zone_with_geojson(db: Session, zone_id: Optional[str] = None, owner_id: Optional[int] = None) -> Optional[Zone]:
    """Get a zone by zone_id and/or owner_id, including GeoJSON polygon."""
    query = select(
        Zone,
        func.ST_AsGeoJSON(Zone.geo_fence_polygon).label("geo_fence_polygon"),
    )
    if zone_id is not None:
        query = query.where(Zone.zone_id == zone_id)
    if owner_id is not None:
        query = query.where(Zone.owner_id == owner_id)
    result = db.execute(query)
    row = result.first()
    if not row:
        return None
    zone, geojson_text = row
    apply_zone_geo_fence_geojson(zone, geojson_text)
    return zone


def list_zones(
    db: Session,
    owner_id: int,
    skip: int = 0,
    limit: int = 100,
    active_only: bool = True,
) -> List[Zone]:
    """List zones for an owner."""
    query = select(Zone).where(Zone.owner_id == owner_id)
    if active_only:
        query = query.where(Zone.active == True)
    query = query.offset(skip).limit(limit)
    result = db.execute(query)
    return result.scalars().all()


def zone_to_dict(zone: Zone) -> dict[str, Any]:
    """Convert a Zone ORM instance into a plain dictionary for serialization."""
    return {
        "id": zone.id,
        "zone_id": zone.zone_id,
        "owner_id": zone.owner_id,
        "creator_id": zone.creator_id,
        "zone_type": zone.zone_type,
        "name": zone.name,
        "description": zone.description,
        "h3_cells": zone.h3_cells,
        "geo_fence_polygon": zone.geo_fence_polygon,
        "parameters": zone.parameters,
        "active": zone.active,
        "created_at": zone.created_at,
        "updated_at": zone.updated_at,
    }


def list_zones_with_geojson(
    db: Session,
    owner_id: int,
    skip: int = 0,
    limit: int = 100,
    active_only: bool = True,
) -> List[Zone]:
    """List zones for an owner and include GeoJSON polygon data."""
    return list_zones_with_geojson_for_owners(
        db,
        [owner_id],
        skip=skip,
        limit=limit,
        active_only=active_only,
    )


def list_zones_with_geojson_for_owners(
    db: Session,
    owner_ids: Sequence[int],
    skip: int = 0,
    limit: int = 100,
    active_only: bool = True,
) -> List[Zone]:
    """List zones for one or more owners and include GeoJSON polygon data."""
    if not owner_ids:
        return []
    query = select(
        Zone,
        func.ST_AsGeoJSON(Zone.geo_fence_polygon).label("geo_fence_polygon"),
    ).where(Zone.owner_id.in_(tuple(owner_ids)))
    if active_only:
        query = query.where(Zone.active == True)
    query = query.order_by(Zone.owner_id.asc(), Zone.id.asc()).offset(skip).limit(limit)
    result = db.execute(query).all()

    zones: List[Zone] = []
    for zone, geojson_text in result:
        apply_zone_geo_fence_geojson(zone, geojson_text)
        zones.append(zone)
    return zones


def list_zones_by_zone_id_with_geojson(
    db: Session,
    zone_id: str,
    skip: int = 0,
    limit: int = 100,
    active_only: bool = True,
) -> List[Zone]:
    """List zones by shared zone_id and include GeoJSON polygon data."""
    query = select(
        Zone,
        func.ST_AsGeoJSON(Zone.geo_fence_polygon).label("geo_fence_polygon"),
    ).where(Zone.zone_id == zone_id)
    if active_only:
        query = query.where(Zone.active == True)
    query = query.offset(skip).limit(limit)
    result = db.execute(query).all()

    zones: List[Zone] = []
    for zone, geojson_text in result:
        apply_zone_geo_fence_geojson(zone, geojson_text)
        zones.append(zone)
    return zones


def update_zone(
    db: Session,
    zone_id: str,
    zone_update: ZoneUpdate,
    owner_id: Optional[int] = None,
) -> Optional[Zone]:
    """Update a zone."""
    db_zone = get_zone(db, zone_id, owner_id)
    if not db_zone:
        return None
    
    update_data = zone_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "zone_type" and value:
            value = ZoneType(value)
        if field == "h3_cells" and value:
            if any(not validate_h3_cell(cell) for cell in value):
                raise ValueError("Invalid H3 cell id")
            if has_h3_overlap(value):
                raise ValueError("Overlapping H3 cells are not allowed across resolutions")
        if field == "geo_fence_polygon":
            if value is None:
                setattr(db_zone, field, None)
                continue
            value = _geojson_to_geometry(value)
        setattr(db_zone, field, value)

    if "h3_cells" in update_data:
        _sync_parameters_h3_cells(db_zone)

    db.flush()
    db.refresh(db_zone)
    return db_zone


def update_zone_by_record_id(
    db: Session,
    record_id: int,
    zone_update: ZoneUpdate,
) -> Optional[Zone]:
    """Update a zone by primary key id."""
    query = select(Zone).where(Zone.id == record_id)
    result = db.execute(query)
    db_zone = result.scalars().first()
    if not db_zone:
        return None

    update_data = zone_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "zone_type" and value:
            value = ZoneType(value)
        if field == "h3_cells" and value:
            if any(not validate_h3_cell(cell) for cell in value):
                raise ValueError("Invalid H3 cell id")
            if has_h3_overlap(value):
                raise ValueError("Overlapping H3 cells are not allowed across resolutions")
        if field == "geo_fence_polygon":
            if value is None:
                setattr(db_zone, field, None)
                continue
            value = _geojson_to_geometry(value)
        setattr(db_zone, field, value)

    if "h3_cells" in update_data:
        _sync_parameters_h3_cells(db_zone)

    db.flush()
    db.refresh(db_zone)
    return db_zone


def get_zone_by_record_id_with_geojson(db: Session, record_id: int) -> Optional[Zone]:
    """Get a zone by primary key id including GeoJSON polygon."""
    query = select(
        Zone,
        func.ST_AsGeoJSON(Zone.geo_fence_polygon).label("geo_fence_polygon"),
    ).where(Zone.id == record_id)
    result = db.execute(query)
    row = result.first()
    if not row:
        return None
    zone, geojson_text = row
    apply_zone_geo_fence_geojson(zone, geojson_text)
    return zone


def delete_zone(db: Session, zone_id: str, owner_id: Optional[int] = None) -> bool:
    """Delete a zone."""
    db_zone = get_zone(db, zone_id, owner_id)
    if not db_zone:
        return False
    
    db.delete(db_zone)
    return True


def count_zones(db: Session, owner_id: int) -> int:
    """Count zones for an owner."""
    result = db.execute(
        select(func.count(Zone.id)).where(Zone.owner_id == owner_id)
    )
    return result.scalar()
