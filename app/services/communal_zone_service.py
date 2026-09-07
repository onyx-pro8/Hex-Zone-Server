"""Communal ID lookup, generation, and public-zone helpers.

Communal IDs group defining zones (geofence, grid, proximity, dynamic,
government_local_code, object). Communal mode itself does not define geometry.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.zone import Zone

_REFERENCE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")


@dataclass(frozen=True)
class ReferenceZoneResolution:
    """Shared resolution shape used by government (and legacy) reference previews."""

    reference_id: str
    display_name: str
    geometry: dict[str, Any]
    config: dict[str, Any]
    h3_cells: list[str]
    source: str

DEFINING_ZONE_TYPES = frozenset(
    {
        "geofence",
        "grid",
        "proximity",
        "dynamic",
        "government_local_code",
        "object",
        "warn",
        "alert",
        "restricted",
        "emergency",
        "custom_2",
    }
)


def normalize_reference_id(raw: str) -> str:
    return str(raw or "").strip().upper()


def is_valid_reference_format(reference_id: str) -> bool:
    normalized = normalize_reference_id(reference_id)
    return bool(normalized) and bool(_REFERENCE_PATTERN.match(normalized))


def zone_parameters(zone: Zone) -> dict[str, Any]:
    params = zone.parameters if isinstance(zone.parameters, dict) else {}
    return params


def zone_config(zone: Zone) -> dict[str, Any]:
    params = zone_parameters(zone)
    config = params.get("config")
    return dict(config) if isinstance(config, dict) else {}


def zone_contract_type(zone: Zone) -> str:
    params = zone_parameters(zone)
    raw = params.get("contractType")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    model = getattr(zone.zone_type, "value", None) or str(zone.zone_type or "")
    return str(model).strip().lower()


def get_communal_id(zone: Zone) -> Optional[str]:
    config = zone_config(zone)
    stored = config.get("communal_id") or config.get("communalId")
    if not isinstance(stored, str) or not stored.strip():
        return None
    return normalize_reference_id(stored)


def is_defining_zone(zone: Zone) -> bool:
    contract = zone_contract_type(zone)
    if contract in {"communal_id", "custom_1"}:
        return False
    if contract in DEFINING_ZONE_TYPES:
        return True
    # Legacy model enums that map to defining types.
    model = getattr(zone.zone_type, "value", None) or str(zone.zone_type or "")
    return model in {
        "geofence",
        "warn",
        "alert",
        "restricted",
        "emergency",
        "custom_2",
    }


def is_zone_public(zone: Zone) -> bool:
    """Defining zones are public unless config.is_public is explicitly false."""
    if not is_defining_zone(zone):
        return False
    config = zone_config(zone)
    flag = config.get("is_public")
    if flag is None:
        flag = config.get("isPublic")
    if flag is False:
        return False
    return bool(zone.active)


def find_zones_by_communal_id(db: Session, reference_id: str) -> list[Zone]:
    normalized = normalize_reference_id(reference_id)
    if not normalized or db is None:
        return []
    zones = db.query(Zone).filter(Zone.active.is_(True)).all()
    matched: list[Zone] = []
    for zone in zones:
        if get_communal_id(zone) == normalized:
            matched.append(zone)
    return matched


def communal_id_exists(db: Session, reference_id: str) -> bool:
    return bool(find_zones_by_communal_id(db, reference_id))


def list_public_defining_zones(
    db: Session,
    *,
    skip: int = 0,
    limit: int = 200,
) -> list[Zone]:
    if db is None:
        return []
    zones = (
        db.query(Zone)
        .filter(Zone.active.is_(True))
        .order_by(Zone.updated_at.desc(), Zone.id.desc())
        .all()
    )
    public = [z for z in zones if is_zone_public(z)]
    return public[skip : skip + limit]


def generate_unique_communal_id(db: Session | None = None) -> str:
    for _ in range(32):
        candidate = f"COMM-{secrets.token_hex(3).upper()}"
        if db is not None and communal_id_exists(db, candidate):
            continue
        return candidate
    return f"COMM-{secrets.token_hex(4).upper()}"


def assign_communal_id(
    zone: Zone,
    reference_id: str,
    *,
    is_public: bool | None = None,
) -> dict[str, Any]:
    """Mutate zone parameters to set communal_id; returns updated config."""
    normalized = normalize_reference_id(reference_id)
    params = dict(zone_parameters(zone))
    config = dict(params.get("config") if isinstance(params.get("config"), dict) else {})
    config["communal_id"] = normalized
    if is_public is not None:
        config["is_public"] = bool(is_public)
    elif "is_public" not in config and "isPublic" not in config:
        config["is_public"] = True
    params["config"] = config
    if "contractType" not in params:
        params["contractType"] = zone_contract_type(zone) or "geofence"
    if "geometry" not in params:
        params["geometry"] = {}
    zone.parameters = params
    return config


# --- Backward-compatible aliases used by older imports / tests -----------------

def generate_communal_reference(db: Session | None, owner_ids: list[int] | None = None):
    """Generate a unique communal ID (no geometry)."""
    del owner_ids  # unused — IDs are globally unique across the DB
    reference_id = generate_unique_communal_id(db)
    return {
        "valid": True,
        "zone_type": "communal_id",
        "reference_id": reference_id,
        "display_name": reference_id,
        "geometry": {},
        "config": {"communal_id": reference_id},
        "h3_cells": [],
        "source": "generated",
        "exists": False,
        "message": f"Generated new Communal ID {reference_id}.",
        "zones": [],
    }


def resolve_communal_reference(
    db: Session | None,
    owner_ids: list[int] | None,
    reference_id: str,
) -> Optional[dict[str, Any]]:
    """Existence check for a communal ID (no invented geometry)."""
    del owner_ids
    normalized = normalize_reference_id(reference_id)
    if not is_valid_reference_format(normalized):
        return None
    if db is None:
        return {
            "valid": False,
            "zone_type": "communal_id",
            "reference_id": normalized,
            "display_name": None,
            "geometry": {},
            "config": {"communal_id": normalized},
            "h3_cells": [],
            "source": "database",
            "exists": False,
            "message": "Communal ID not found.",
            "zones": [],
        }
    matched = find_zones_by_communal_id(db, normalized)
    exists = bool(matched)
    return {
        "valid": exists,
        "zone_type": "communal_id",
        "reference_id": normalized,
        "display_name": matched[0].name if matched else None,
        "geometry": {},
        "config": {"communal_id": normalized},
        "h3_cells": [],
        "source": "database",
        "exists": exists,
        "message": (
            f"Communal ID found on {len(matched)} zone(s)."
            if exists
            else "Communal ID not found. You can generate a new one."
        ),
        "zones": matched,
    }


def resolution_to_response_payload(resolution: dict[str, Any]) -> dict[str, Any]:
    return {
        "valid": bool(resolution.get("valid")),
        "zone_type": resolution.get("zone_type", "communal_id"),
        "reference_id": resolution.get("reference_id", ""),
        "display_name": resolution.get("display_name"),
        "geometry": resolution.get("geometry") or {},
        "config": resolution.get("config") or {},
        "h3_cells": resolution.get("h3_cells") or [],
        "source": resolution.get("source"),
        "exists": bool(resolution.get("exists")),
        "message": resolution.get("message"),
        "zones": resolution.get("zones") or [],
    }
