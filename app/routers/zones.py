"""Router for Zone endpoints."""
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.h3_utils import has_h3_overlap, validate_h3_cell
from app.core.security import get_current_user
from app.crud import owner as owner_crud
from app.crud import zone as zone_crud
from app.database import get_db
from app.models.zone import Zone, ZoneType
from app.services.access_policy import visible_zone_owner_ids
from app.services.communal_zone_service import (
    generate_communal_reference,
    is_valid_reference_format,
    normalize_reference_id,
    resolution_to_response_payload as communal_resolution_to_response_payload,
    resolve_communal_reference,
)
from app.services.government_zone_service import (
    is_valid_local_code_format,
    normalize_local_area_code,
    resolution_to_response_payload as government_resolution_to_response_payload,
    resolve_government_local_code,
)
from app.services.zone_policy import (
    account_owner_ids_for_policy,
    build_capabilities,
    count_zones_for_owners,
    enforce_can_create,
    ensure_unique_zone_name,
    ensure_zone_edit_allowed,
    normalize_zone_name,
)

router = APIRouter(prefix="/zones", tags=["zones"])

CANONICAL_ZONE_TYPES = {
    "geofence",
    "grid",
    "communal_id",
    "government_local_code",
    "object",
    "emergency",
    "warn",
    "alert",
    "restricted",
    "proximity",
    "dynamic",
    "custom_1",
    "custom_2",
}

ZONE_TYPE_ALIASES = {
    "polygon": "geofence",
    "circle": "proximity",
    "custom_1": "communal_id",
    "custom_2": "government_local_code",
    "local_code": "government_local_code",
    "gov_local_code": "government_local_code",
    "warn": "grid",
    "alert": "grid",
    "restricted": "proximity",
    "emergency": "dynamic",
}

CANONICAL_TO_MODEL_ZONE_TYPE = {
    "geofence": ZoneType.GEOFENCE,
    "grid": ZoneType.ALERT,
    "communal_id": ZoneType.CUSTOM_1,
    "government_local_code": ZoneType.CUSTOM_2,
    "object": ZoneType.CUSTOM_2,
    "emergency": ZoneType.EMERGENCY,
    "warn": ZoneType.WARN,
    "alert": ZoneType.ALERT,
    "restricted": ZoneType.RESTRICTED,
    "proximity": ZoneType.RESTRICTED,
    "dynamic": ZoneType.EMERGENCY,
    "custom_1": ZoneType.CUSTOM_1,
    "custom_2": ZoneType.CUSTOM_2,
}

MODEL_TO_CANONICAL_ZONE_TYPE = {
    ZoneType.GEOFENCE: "geofence",
    ZoneType.WARN: "grid",
    ZoneType.ALERT: "grid",
    ZoneType.RESTRICTED: "proximity",
    ZoneType.EMERGENCY: "dynamic",
    ZoneType.CUSTOM_1: "communal_id",
    ZoneType.CUSTOM_2: "government_local_code",
}


class ZoneContractCreate(BaseModel):
    """Create payload for aligned zone contract."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "examples": [
                {
                    "name": "create geofence",
                    "type": "geofence",
                    "geometry": {
                        "geo_fence_polygon": {
                            "type": "Polygon",
                            "coordinates": [[[106.8, -6.2], [106.9, -6.2], [106.9, -6.3], [106.8, -6.2]]],
                        }
                    },
                    "config": {"h3_cells": ["8928308280fffff"]},
                },
                {
                    "name": "create proximity with multi centers",
                    "type": "proximity",
                    "geometry": {
                        "center": {"latitude": -6.2001, "longitude": 106.8167},
                        "centers": [
                            {"latitude": -6.2001, "longitude": 106.8167},
                            {"latitude": -6.2020, "longitude": 106.8180},
                        ],
                    },
                    "config": {"radius_meters": 120},
                },
                {
                    "name": "create custom_1",
                    "type": "custom_1",
                    "geometry": {},
                    "config": {"communal_id": "COMM-77"},
                },
            ]
        },
    )

    name: str = Field(..., min_length=1, max_length=255)
    type: Optional[str] = None
    zone_type: Optional[str] = None
    geometry: Optional[dict[str, Any]] = None
    config: Optional[dict[str, Any]] = None
    h3_cells: Optional[list[str]] = None
    geo_fence_polygon: Optional[dict[str, Any]] = None
    zone_id: Optional[str] = None


class ZoneContractUpdate(BaseModel):
    """Update payload for aligned zone contract."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "examples": [
                {
                    "type": "dynamic",
                    "geometry": {
                        "center": {"latitude": -6.2001, "longitude": 106.8167},
                        "centers": [
                            {"latitude": -6.2001, "longitude": 106.8167},
                            {"latitude": -6.2020, "longitude": 106.8180},
                        ],
                    },
                    "config": {"min_radius_meters": 50, "max_radius_meters": 250},
                }
            ]
        },
    )

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    type: Optional[str] = None
    zone_type: Optional[str] = None
    geometry: Optional[dict[str, Any]] = None
    config: Optional[dict[str, Any]] = None
    h3_cells: Optional[list[str]] = None
    geo_fence_polygon: Optional[dict[str, Any]] = None


class ZoneContractResponse(BaseModel):
    """Canonical zone shape returned to frontend."""

    id: int
    zone_id: str
    owner_id: int
    name: str
    type: str
    geometry: dict[str, Any]
    config: dict[str, Any]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": 42,
                "zone_id": "ZONE-7A29",
                "owner_id": 9,
                "name": "Warehouse Perimeter",
                "type": "geofence",
                "geometry": {"geo_fence_polygon": {"type": "Polygon", "coordinates": [[[106.8, -6.2], [106.9, -6.2], [106.9, -6.3], [106.8, -6.2]]]}},
                "config": {"h3_cells": ["8928308280fffff"]},
                "created_at": "2026-04-23T09:00:00",
                "updated_at": "2026-04-23T09:10:00",
            }
        }
    )


class ZoneReferenceValidateRequest(BaseModel):
    zone_type: str = Field(description="communal_id (Type 2) or government_local_code (Type 3)")
    reference_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Legacy short code, or auto-derived reference id for global addresses.",
    )
    address_mode: Optional[str] = Field(
        default=None,
        description="postal | street — global government local area (Type 3).",
    )
    postal_code: Optional[str] = Field(default=None, max_length=32)
    city: Optional[str] = Field(default=None, max_length=120)
    country: Optional[str] = Field(default=None, max_length=120)
    street: Optional[str] = Field(default=None, max_length=200)
    street_number: Optional[str] = Field(default=None, max_length=32)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "zone_type": "communal_id",
                    "reference_id": "COMM-77",
                },
                {
                    "zone_type": "government_local_code",
                    "address_mode": "postal",
                    "postal_code": "M5H 2N2",
                    "city": "Toronto",
                    "country": "Canada",
                },
                {
                    "zone_type": "government_local_code",
                    "address_mode": "street",
                    "street": "Queen Street West",
                    "street_number": "100",
                    "postal_code": "M5H 2N2",
                    "city": "Toronto",
                    "country": "Canada",
                },
            ]
        }
    )


class ZoneReferenceValidateResponse(BaseModel):
    valid: bool
    zone_type: str
    reference_id: str
    display_name: Optional[str] = None
    geometry: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    h3_cells: list[str] = Field(default_factory=list)
    source: Optional[str] = None
    message: Optional[str] = None


class ZoneReferenceGenerateRequest(BaseModel):
    zone_type: str = Field(default="communal_id")

    model_config = ConfigDict(
        json_schema_extra={"example": {"zone_type": "communal_id"}}
    )


class ZoneCapabilitiesResponse(BaseModel):
    role: str
    can_create_zone: bool
    remaining_total: int
    remaining_for_role: int
    max_total: int
    reserved_for_standard_users: int
    reason: Optional[str] = None


def _normalize_zone_type(raw_type: Optional[str]) -> Optional[str]:
    if raw_type is None:
        return None
    normalized = str(raw_type).strip().lower()
    normalized = ZONE_TYPE_ALIASES.get(normalized, normalized)
    return normalized


def _extract_h3_cells(config: dict[str, Any]) -> list[str]:
    value = config.get("h3_cells")
    if value is None:
        value = config.get("h3Cells")
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="config.h3_cells must be an array",
        )
    if any(not isinstance(cell, str) or not cell.strip() for cell in value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="config.h3_cells must contain non-empty H3 strings",
        )
    return value


def _extract_geo_fence_polygon(geometry: dict[str, Any]) -> Optional[dict[str, Any]]:
    polygon = geometry.get("geo_fence_polygon")
    if polygon is not None and not isinstance(polygon, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="geometry.geo_fence_polygon must be an object",
        )
    return polygon


def _validate_zone_payload(zone_type: str, geometry: dict[str, Any], config: dict[str, Any]) -> None:
    h3_cells = _extract_h3_cells(config)
    if h3_cells:
        if any(not validate_h3_cell(cell) for cell in h3_cells):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid H3 cell id")
        if has_h3_overlap(h3_cells):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Overlapping H3 cells are not allowed across resolutions",
            )

    if zone_type in {"geofence", "grid", "warn", "alert", "restricted"}:
        polygon = _extract_geo_fence_polygon(geometry)
        if not polygon and not h3_cells:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "message": f"{zone_type} requires geometry.geo_fence_polygon or config.h3_cells",
                    "error_code": "ZONE_VALIDATION_FAILED",
                    "details": {"type": zone_type, "required_any_of": ["geometry.geo_fence_polygon", "config.h3_cells"]},
                },
            )
        return

    if zone_type == "proximity":
        center = geometry.get("center")
        if not isinstance(center, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="proximity requires geometry.center with latitude and longitude",
            )
        latitude = center.get("latitude")
        longitude = center.get("longitude")
        if not isinstance(latitude, (float, int)) or not isinstance(longitude, (float, int)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="proximity requires numeric geometry.center.latitude and geometry.center.longitude",
            )
        centers = geometry.get("centers")
        if centers is not None:
            if not isinstance(centers, list):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="proximity geometry.centers must be an array when provided",
                )
            for idx, item in enumerate(centers):
                if not isinstance(item, dict) or not isinstance(item.get("latitude"), (float, int)) or not isinstance(item.get("longitude"), (float, int)):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"proximity geometry.centers[{idx}] must include numeric latitude and longitude",
                    )
        radius = config.get("radius_meters")
        if not isinstance(radius, (float, int)) or radius <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="proximity requires config.radius_meters > 0",
            )
        return

    if zone_type == "dynamic":
        center = geometry.get("center")
        if not isinstance(center, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="dynamic requires geometry.center with latitude and longitude",
            )
        if not isinstance(center.get("latitude"), (float, int)) or not isinstance(center.get("longitude"), (float, int)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="dynamic requires numeric geometry.center.latitude and geometry.center.longitude",
            )
        centers = geometry.get("centers")
        if centers is not None:
            if not isinstance(centers, list):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="dynamic geometry.centers must be an array when provided",
                )
            for idx, item in enumerate(centers):
                if not isinstance(item, dict) or not isinstance(item.get("latitude"), (float, int)) or not isinstance(item.get("longitude"), (float, int)):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"dynamic geometry.centers[{idx}] must include numeric latitude and longitude",
                    )
        min_radius = config.get("min_radius_meters")
        max_radius = config.get("max_radius_meters")
        if not isinstance(min_radius, (float, int)) or min_radius <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="dynamic requires config.min_radius_meters > 0",
            )
        if not isinstance(max_radius, (float, int)) or max_radius < min_radius:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="dynamic requires config.max_radius_meters >= config.min_radius_meters",
            )
        return

    if zone_type in {"communal_id", "custom_1"}:
        communal_id = config.get("communal_id")
        if not isinstance(communal_id, str) or not communal_id.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="communal_id requires non-empty config.communal_id",
            )
        return

    if zone_type in {"government_local_code", "custom_2"}:
        local_code = config.get("local_code")
        if not isinstance(local_code, str) or not local_code.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="government_local_code requires non-empty config.local_code",
            )
        return

    if zone_type == "object":
        object_id = config.get("object_id")
        if not isinstance(object_id, str) or not object_id.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="object requires non-empty config.object_id",
            )
        radius = config.get("radius_meters")
        if not isinstance(radius, (float, int)) or radius <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="object requires config.radius_meters > 0",
            )
        center = geometry.get("center")
        if not isinstance(center, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="object requires geometry.center with latitude and longitude",
            )
        latitude = center.get("latitude")
        longitude = center.get("longitude")
        if not isinstance(latitude, (float, int)) or not isinstance(longitude, (float, int)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="object requires numeric geometry.center.latitude and geometry.center.longitude",
            )
        return


def _normalize_payload(payload: dict[str, Any], partial: bool) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    if not partial or "name" in payload:
        name = payload.get("name")
        if not partial and not name:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name is required")
        if name is not None:
            normalized["name"] = name

    incoming_type = payload.get("type") if payload.get("type") is not None else payload.get("zone_type")
    normalized_type = _normalize_zone_type(incoming_type)
    if normalized_type is not None:
        if normalized_type not in CANONICAL_ZONE_TYPES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported zone type")
        normalized["type"] = normalized_type
    elif not partial:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="type or zone_type is required")

    geometry_supplied = "geometry" in payload or "geo_fence_polygon" in payload
    if geometry_supplied:
        geometry = payload.get("geometry")
        if geometry is None:
            geometry = {}
        if not isinstance(geometry, dict):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="geometry must be an object")
        if "geo_fence_polygon" in payload and "geo_fence_polygon" not in geometry:
            legacy_polygon = payload.get("geo_fence_polygon")
            if legacy_polygon is not None:
                if not isinstance(legacy_polygon, dict):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="geo_fence_polygon must be an object",
                    )
                geometry["geo_fence_polygon"] = legacy_polygon
        normalized["geometry"] = geometry
    elif not partial:
        normalized["geometry"] = {}

    config_supplied = "config" in payload or "h3_cells" in payload
    if config_supplied:
        config = payload.get("config")
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="config must be an object")
        if "h3Cells" in config and "h3_cells" not in config:
            config["h3_cells"] = config.get("h3Cells")
        if "h3_cells" in payload and "h3_cells" not in config:
            config["h3_cells"] = payload.get("h3_cells")
        normalized["config"] = config
    elif not partial:
        normalized["config"] = {}

    return normalized


def _serialize_zone(zone: Zone) -> dict[str, Any]:
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    geometry = params.get("geometry") if isinstance(params.get("geometry"), dict) else {}
    config = params.get("config") if isinstance(params.get("config"), dict) else {}
    config = dict(config)

    if "h3Cells" in config and "h3_cells" not in config:
        config["h3_cells"] = config["h3Cells"]
    if zone.h3_cells and "h3_cells" not in config:
        config["h3_cells"] = list(zone.h3_cells)

    polygon = zone.geo_fence_polygon if isinstance(zone.geo_fence_polygon, dict) else None
    if polygon and "geo_fence_polygon" not in geometry:
        geometry = dict(geometry)
        geometry["geo_fence_polygon"] = polygon

    zone_type = params.get("contractType")
    normalized_type = _normalize_zone_type(zone_type) if zone_type else None
    if not normalized_type:
        normalized_type = MODEL_TO_CANONICAL_ZONE_TYPE.get(zone.zone_type, "geofence")

    return {
        "id": zone.id,
        "zone_id": zone.zone_id,
        "owner_id": zone.owner_id,
        "name": zone.name,
        "type": normalized_type,
        "geometry": geometry if isinstance(geometry, dict) else {},
        "config": config if isinstance(config, dict) else {},
        "created_at": zone.created_at.isoformat() if zone.created_at else None,
        "updated_at": zone.updated_at.isoformat() if zone.updated_at else None,
    }


@router.post(
    "/",
    response_model=ZoneContractResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create zone",
    description="Create a canonical zone and accept legacy compatibility fields during transition.",
    responses={
        404: {
            "description": "Authenticated owner not found.",
        },
        403: {
            "description": "Forbidden create",
            "content": {
                "application/json": {
                    "example": {
                        "status": "error",
                        "message": "User can only configure Zone #2 and Zone #3",
                        "error_code": "HTTP_403",
                    }
                }
            },
        },
        422: {
            "description": "Validation failed",
            "content": {
                "application/json": {
                    "example": {
                        "status": "error",
                        "message": "proximity requires config.radius_meters > 0",
                        "error_code": "HTTP_422",
                    }
                }
            },
        },
        500: {
            "description": "Zone was created but could not be loaded for response.",
        },
    },
    response_description="Created zone in canonical response shape.",
)
async def create_zone(
    zone: ZoneContractCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new zone for the current owner."""
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )

    normalized = _normalize_payload(zone.model_dump(exclude_none=True), partial=False)

    account_owner_ids = account_owner_ids_for_policy(db, owner)
    total_zones = count_zones_for_owners(db, account_owner_ids)
    capabilities = build_capabilities(owner.role.value, total_zones)
    enforce_can_create(capabilities)

    geometry = normalized.get("geometry", {})
    config = normalized.get("config", {})
    zone_type = normalized["type"]
    normalized_name = normalize_zone_name(normalized["name"])
    ensure_unique_zone_name(db, account_owner_ids, normalized_name)
    _validate_zone_payload(zone_type, geometry, config)

    model_zone_type = CANONICAL_TO_MODEL_ZONE_TYPE[zone_type]
    h3_cells = _extract_h3_cells(config)
    geo_fence_polygon = _extract_geo_fence_polygon(geometry)

    db_zone = Zone(
        zone_id=zone.zone_id or owner.zone_id,
        owner_id=owner.id,
        creator_id=owner.id,
        zone_type=model_zone_type,
        name=normalized_name,
        parameters={
            "contractType": zone_type,
            "geometry": geometry,
            "config": config,
        },
        h3_cells=h3_cells,
        geo_fence_polygon=geo_fence_polygon,
    )
    db.add(db_zone)
    db.flush()
    db.commit()
    created_zone = zone_crud.get_zone_by_record_id_with_geojson(db, db_zone.id)
    if not created_zone:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve created zone")
    return ZoneContractResponse.model_validate(_serialize_zone(created_zone))


@router.get(
    "/",
    response_model=list[ZoneContractResponse],
    summary="List zones",
    description="List caller-visible zones in canonical shape.",
    responses={
        403: {
            "description": "Forbidden access for requested owner_id.",
        },
        404: {
            "description": "Authenticated owner not found.",
        },
    },
    response_description="List of canonical zone objects visible to caller.",
)
async def list_zones(
    skip: int = Query(0, ge=0, description="Pagination offset."),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of zones to return."),
    owner_id: Optional[int] = Query(
        None,
        ge=1,
        description=(
            "Filter by owner record id. Allowed only when the requested owner is visible "
            "to the authenticated caller."
        ),
    ),
    zone_id: Optional[str] = Query(
        None,
        min_length=1,
        description=(
            "Filter by shared zone identifier (`zone.zone_id` string). "
            "When provided, returns all caller-visible entries with that shared id."
        ),
    ),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List zones, or all matching shared zone_id entries when provided."""
    caller = owner_crud.get_owner(db, current_user["user_id"])
    if not caller:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )
    allowed_ids = set(visible_zone_owner_ids(db, caller))

    if zone_id is not None:
        zones = zone_crud.list_zones_by_zone_id_with_geojson(
            db,
            zone_id=zone_id,
            skip=skip,
            limit=limit,
        )
        zones = [zone for zone in zones if zone.owner_id in allowed_ids]
        return [ZoneContractResponse.model_validate(_serialize_zone(zone)) for zone in zones]

    if owner_id is not None and owner_id not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: cannot access another owner's zones",
        )

    target_owner_ids = [owner_id] if owner_id is not None else sorted(allowed_ids)
    zones = zone_crud.list_zones_with_geojson_for_owners(
        db,
        owner_ids=target_owner_ids,
        skip=skip,
        limit=limit,
    )
    return [ZoneContractResponse.model_validate(_serialize_zone(zone)) for zone in zones]


@router.get(
    "/capabilities",
    response_model=ZoneCapabilitiesResponse,
    summary="Get zone capabilities for caller",
    description="Expose role-aware zone creation capabilities for frontend UX decisions.",
)
async def get_zone_capabilities(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Owner not found", "error_code": "OWNER_NOT_FOUND"},
        )
    owner_ids = account_owner_ids_for_policy(db, owner)
    total_zones = count_zones_for_owners(db, owner_ids)
    return ZoneCapabilitiesResponse.model_validate(build_capabilities(owner.role.value, total_zones).to_dict())


def _resolve_reference_zone_type(raw: str) -> str:
    normalized = _normalize_zone_type(raw)
    if normalized in {"communal_id", "custom_1"}:
        return "communal_id"
    if normalized in {"government_local_code", "custom_2"}:
        return "government_local_code"
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="zone_type must be communal_id or government_local_code",
    )


@router.post(
    "/validate-reference",
    response_model=ZoneReferenceValidateResponse,
    summary="Validate communal / government reference ID",
    description=(
        "Type 2–3: resolve a reference ID to zone geometry for map preview before save. "
        "Returns geo_fence_polygon and optional h3_cells."
    ),
)
async def validate_zone_reference(
    body: ZoneReferenceValidateRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")

    resolved_type = _resolve_reference_zone_type(body.zone_type)
    owner_ids = account_owner_ids_for_policy(db, owner)

    if resolved_type == "communal_id":
        reference_id = normalize_reference_id(body.reference_id)
        if not is_valid_reference_format(reference_id):
            return ZoneReferenceValidateResponse(
                valid=False,
                zone_type=resolved_type,
                reference_id=reference_id,
                message="Reference ID must be 3–32 characters (letters, numbers, hyphen, underscore).",
            )
        resolution = resolve_communal_reference(db, owner_ids, reference_id)
        if not resolution:
            return ZoneReferenceValidateResponse(
                valid=False,
                zone_type=resolved_type,
                reference_id=reference_id,
                message="Communal ID could not be resolved.",
            )
        payload = communal_resolution_to_response_payload(resolution)
        return ZoneReferenceValidateResponse.model_validate(payload)

    address_payload = {
        "address_mode": body.address_mode,
        "postal_code": body.postal_code,
        "city": body.city,
        "country": body.country,
        "street": body.street,
        "street_number": body.street_number,
    }
    has_global_address = bool(str(body.country or "").strip()) and (
        bool(str(body.postal_code or "").strip())
        or bool(str(body.city or "").strip())
        or bool(str(body.street or "").strip())
    )

    if has_global_address:
        from app.services.area_boundary_service import (
            boundary_lookup_status,
            parse_area_location_from_dict,
        )

        lookup_ok, lookup_reason = boundary_lookup_status()
        if not lookup_ok:
            return ZoneReferenceValidateResponse(
                valid=False,
                zone_type=resolved_type,
                reference_id="",
                message=(
                    "Area boundary lookup is disabled on the server. "
                    "Set BOUNDARY_LOOKUP_ENABLED=true and restart the API."
                ),
            )

        location = parse_area_location_from_dict(address_payload)
        ref_id = location.reference_id() if location else ""
        resolution = resolve_government_local_code(
            db,
            owner_ids,
            ref_id,
            address=address_payload,
        )
        if not resolution:
            from app.services.area_boundary_service import get_last_boundary_lookup_failure

            failure = get_last_boundary_lookup_failure()
            if failure == "boundary_lookup_disabled":
                detail = (
                    "Area boundary lookup is disabled on the server "
                    "(BOUNDARY_LOOKUP_ENABLED=false)."
                )
            elif failure.startswith("nominatim_unreachable") or failure.startswith(
                "photon_unreachable"
            ):
                detail = (
                    "The server could not reach geocoding services (OpenStreetMap/Photon). "
                    "Check outbound HTTPS from the API host or try again later."
                )
            elif failure.startswith("nominatim_http_"):
                detail = (
                    "OpenStreetMap rejected the boundary request (rate limit or blocked). "
                    "Wait a few seconds and try again."
                )
            else:
                detail = (
                    "Could not resolve this address to an area boundary. "
                    "Check postal code, city, and country (e.g. 00510, Helsinki, Finland)."
                )
            return ZoneReferenceValidateResponse(
                valid=False,
                zone_type=resolved_type,
                reference_id=ref_id,
                message=detail,
            )
        payload = government_resolution_to_response_payload(resolution)
        return ZoneReferenceValidateResponse.model_validate(payload)

    reference_raw = str(body.reference_id or "").strip()
    if not reference_raw:
        return ZoneReferenceValidateResponse(
            valid=False,
            zone_type=resolved_type,
            reference_id="",
            message=(
                "Provide postal code, city, and country — or street, postal code, "
                "city, and country."
            ),
        )

    local_code = normalize_local_area_code(reference_raw)
    if not is_valid_local_code_format(local_code):
        return ZoneReferenceValidateResponse(
            valid=False,
            zone_type=resolved_type,
            reference_id=local_code,
            message=(
                "Provide postal code, city, and country (global), "
                "or a legacy district code (e.g. ID-JK-3171)."
            ),
        )
    resolution = resolve_government_local_code(db, owner_ids, local_code)
    if not resolution:
        return ZoneReferenceValidateResponse(
            valid=False,
            zone_type=resolved_type,
            reference_id=local_code,
            message="Local area code could not be resolved.",
        )
    payload = government_resolution_to_response_payload(resolution)
    return ZoneReferenceValidateResponse.model_validate(payload)


@router.post(
    "/generate-reference",
    response_model=ZoneReferenceValidateResponse,
    summary="Generate a new communal reference ID",
    description="Type 2: server generates a communal ID and resolvable preview geometry.",
)
async def generate_zone_reference(
    body: ZoneReferenceGenerateRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")

    resolved_type = _resolve_reference_zone_type(body.zone_type)
    if resolved_type != "communal_id":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only communal_id generation is supported",
        )

    owner_ids = account_owner_ids_for_policy(db, owner)
    resolution = generate_communal_reference(db, owner_ids)
    payload = communal_resolution_to_response_payload(resolution)
    return ZoneReferenceValidateResponse.model_validate(payload)


@router.get(
    "/{zone_id}",
    response_model=ZoneContractResponse,
    summary="Get zone by record ID",
    description="Return a single canonical zone by DB record id.",
    responses={
        403: {
            "description": "Forbidden access to requested zone.",
        },
        404: {
            "description": "Owner or zone not found.",
        },
        422: {
            "description": "Invalid zone id path parameter.",
        },
    },
    response_description="Canonical zone object for the requested DB record.",
)
async def get_zone(
    zone_id: int = Path(..., ge=1, description="Zone DB record id (`zone.id`)."),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a single zone by DB record id for authenticated users."""
    caller = owner_crud.get_owner(db, current_user["user_id"])
    if not caller:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )
    allowed_ids = set(visible_zone_owner_ids(db, caller))
    zone = zone_crud.get_zone_by_record_id_with_geojson(db, zone_id)
    if not zone:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Zone not found", "error_code": "ZONE_NOT_FOUND"},
        )
    if zone.owner_id not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": "Forbidden: cannot access this zone", "error_code": "ZONE_FORBIDDEN"},
        )
    return ZoneContractResponse.model_validate(_serialize_zone(zone))


@router.patch(
    "/{zone_id}",
    response_model=ZoneContractResponse,
    summary="Update zone",
    description="Patch canonical zone by DB record id.",
    responses={
        404: {
            "description": "Owner or zone not found.",
        },
        403: {
            "description": "Forbidden update",
            "content": {
                "application/json": {
                    "example": {
                        "status": "error",
                        "message": "Forbidden: users can edit only zones they created",
                        "error_code": "HTTP_403",
                    }
                }
            },
        },
        422: {
            "description": "Validation failed",
            "content": {
                "application/json": {
                    "example": {
                        "status": "error",
                        "message": "dynamic requires config.max_radius_meters >= config.min_radius_meters",
                        "error_code": "HTTP_422",
                    }
                }
            },
        },
        500: {
            "description": "Zone update was committed but could not be loaded for response.",
        },
    },
    response_description="Updated canonical zone object.",
)
async def update_zone(
    zone_update: ZoneContractUpdate,
    zone_id: int = Path(..., ge=1, description="Zone DB record id (`zone.id`)."),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a zone by record id."""
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )

    target_zone = zone_crud.get_zone_by_record_id_with_geojson(db, zone_id)
    if not target_zone:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Zone not found",
        )

    ensure_zone_edit_allowed(owner, target_zone)

    normalized = _normalize_payload(zone_update.model_dump(exclude_none=True), partial=True)
    current = _serialize_zone(target_zone)
    merged = {
        "name": normalized.get("name", target_zone.name),
        "type": normalized.get("type", current["type"]),
        "geometry": normalized.get("geometry", current["geometry"]),
        "config": normalized.get("config", current["config"]),
    }

    normalized_name = normalize_zone_name(merged["name"])
    owner_ids = account_owner_ids_for_policy(db, owner)
    ensure_unique_zone_name(db, owner_ids, normalized_name, exclude_zone_record_id=target_zone.id)
    _validate_zone_payload(merged["type"], merged["geometry"], merged["config"])

    target_zone.name = normalized_name
    target_zone.zone_type = CANONICAL_TO_MODEL_ZONE_TYPE[merged["type"]]
    target_zone.h3_cells = _extract_h3_cells(merged["config"])
    target_zone.geo_fence_polygon = _extract_geo_fence_polygon(merged["geometry"])
    target_zone.parameters = {
        "contractType": merged["type"],
        "geometry": merged["geometry"],
        "config": merged["config"],
    }

    db.flush()
    db.commit()
    updated_zone = zone_crud.get_zone_by_record_id_with_geojson(db, zone_id)
    if not updated_zone:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve updated zone",
        )
    return ZoneContractResponse.model_validate(_serialize_zone(updated_zone))


@router.delete(
    "/{zone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete zone",
    description=(
        "Delete a zone owned by the authenticated caller. "
        "This route accepts shared zone identifiers (zone_id string), not DB record IDs."
    ),
    responses={
        422: {
            "description": "Invalid shared zone identifier.",
        },
        404: {
            "description": "Zone not found for the authenticated owner.",
        },
    },
    response_description="Zone deleted successfully.",
)
async def delete_zone(
    zone_id: str = Path(
        ...,
        min_length=1,
        description=(
            "Shared zone identifier string (`zone.zone_id`) for the caller-owned zone "
            "to remove."
        ),
    ),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a zone."""
    deleted = zone_crud.delete_zone(db, zone_id, owner_id=current_user["user_id"])
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Zone not found",
        )
    db.commit()
