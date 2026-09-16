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


def zone_communal_id_taken(db: Session, reference_id: str) -> bool:
    """True when any active zone config already stores this Communal ID."""
    normalized = normalize_reference_id(reference_id)
    if not normalized or db is None:
        return False
    # Select only id + parameters to avoid PostGIS geometry (AsEWKB) on SQLite.
    rows = (
        db.query(Zone.id, Zone.parameters)
        .filter(Zone.active.is_(True))
        .all()
    )
    for _, parameters in rows:
        params = parameters if isinstance(parameters, dict) else {}
        config = params.get("config") if isinstance(params.get("config"), dict) else {}
        stored = config.get("communal_id") or config.get("communalId")
        if isinstance(stored, str) and normalize_reference_id(stored) == normalized:
            return True
    return False


def find_zones_by_communal_id(db: Session, reference_id: str) -> list[Zone]:
    normalized = normalize_reference_id(reference_id)
    if not normalized or db is None:
        return []
    rows = (
        db.query(Zone.id, Zone.parameters)
        .filter(Zone.active.is_(True))
        .all()
    )
    matched_ids: list[int] = []
    for zone_id, parameters in rows:
        params = parameters if isinstance(parameters, dict) else {}
        config = params.get("config") if isinstance(params.get("config"), dict) else {}
        stored = config.get("communal_id") or config.get("communalId")
        if isinstance(stored, str) and normalize_reference_id(stored) == normalized:
            matched_ids.append(int(zone_id))
    if not matched_ids:
        return []
    try:
        return (
            db.query(Zone)
            .filter(Zone.id.in_(tuple(matched_ids)))
            .all()
        )
    except Exception:
        # SQLite / missing spatial functions: return lightweight stand-ins unused
        # by uniqueness checks (callers that need geometry use Postgres).
        return []


def owner_communal_id_taken(db: Session, reference_id: str) -> bool:
    """True when another owner already holds this assigned Communal ID."""
    from app.models import Owner

    normalized = normalize_reference_id(reference_id)
    if not normalized or db is None:
        return False
    return (
        db.query(Owner.id)
        .filter(Owner.communal_id == normalized)
        .first()
        is not None
    )


def qr_invite_communal_id_taken(db: Session, reference_id: str) -> bool:
    """True when a member-invite QR already reserved this Communal ID."""
    from app.models import QRRegistration

    normalized = normalize_reference_id(reference_id)
    if not normalized or db is None:
        return False
    return (
        db.query(QRRegistration.id)
        .filter(QRRegistration.communal_id == normalized)
        .first()
        is not None
    )


def communal_id_exists(db: Session, reference_id: str) -> bool:
    """True when the ID is used on a zone, owner, or pending/used QR invite."""
    return (
        zone_communal_id_taken(db, reference_id)
        or owner_communal_id_taken(db, reference_id)
        or qr_invite_communal_id_taken(db, reference_id)
    )


def mint_invite_communal_id(db: Session) -> str:
    """Mint a unique Communal ID reserved for a member-invite QR."""
    for _ in range(32):
        candidate = generate_unique_communal_id(db)
        if qr_invite_communal_id_taken(db, candidate):
            continue
        if owner_communal_id_taken(db, candidate):
            continue
        return candidate
    return f"COMM-{secrets.token_hex(4).upper()}"


def zone_network_id(zone: Zone) -> str:
    return str(getattr(zone, "zone_id", None) or "").strip()


def caller_network_id(owner) -> str:
    return str(getattr(owner, "zone_id", None) or "").strip()


def zone_eligible_for_communal_assignment(owner, zone: Zone) -> bool:
    """True when the caller may attach a Communal ID to this defining zone.

    Network admins and members may only use **primary** zones in their own
    network. Solo Individual accounts (no primary tier) may use defining zones
    they created in their network. System administrators may use any public
    defining zone.
    """
    from app.services.account_type_policy import (
        is_individual_account_type,
        is_system_administrator,
    )
    from app.services.zone_policy import owner_is_invited_member, zone_is_primary

    if not is_zone_public(zone):
        return False
    if is_system_administrator(owner):
        return True

    network = caller_network_id(owner)
    if not network or zone_network_id(zone) != network:
        return False

    if zone_is_primary(zone):
        return True

    # Solo Individuals never create primary zones — allow their own defining zones.
    # Invited members (also Individual account type) stay primary-only.
    if is_individual_account_type(getattr(owner, "account_type", None)) and not owner_is_invited_member(
        owner
    ):
        return int(getattr(zone, "creator_id", 0) or 0) == int(owner.id)

    return False


def list_public_defining_zones(
    db: Session,
    *,
    owner=None,
    skip: int = 0,
    limit: int = 200,
) -> list[Zone]:
    """List defining zones eligible for Communal ID selection.

    Scoped to the caller's network primary zones (not every network). System
    administrators still see all public defining zones.
    """
    if db is None:
        return []
    query = db.query(Zone).filter(Zone.active.is_(True))
    if owner is not None:
        from app.services.account_type_policy import is_system_administrator

        if not is_system_administrator(owner):
            network = caller_network_id(owner)
            if not network:
                return []
            query = query.filter(Zone.zone_id == network)

    zones = query.order_by(Zone.updated_at.desc(), Zone.id.desc()).all()
    if owner is None:
        public = [z for z in zones if is_zone_public(z)]
    else:
        public = [z for z in zones if zone_eligible_for_communal_assignment(owner, z)]
    return public[skip : skip + limit]


def generate_unique_communal_id(db: Session | None = None) -> str:
    for _ in range(32):
        candidate = f"COMM-{secrets.token_hex(3).upper()}"
        if db is not None and communal_id_exists(db, candidate):
            continue
        return candidate
    return f"COMM-{secrets.token_hex(4).upper()}"


def assign_owner_communal_id(db: Session, owner) -> str:
    """Ensure an Individual (exclusive) owner has a unique assigned Communal ID.

    Returns the owner's communal_id. Non-Individual owners are left unchanged
    (returns empty string when none is set).
    """
    from app.services.account_type_policy import is_individual_account_type

    existing = normalize_reference_id(getattr(owner, "communal_id", None) or "")
    if existing:
        return existing
    if not is_individual_account_type(getattr(owner, "account_type", None)):
        return ""

    for _ in range(32):
        candidate = generate_unique_communal_id(db)
        # generate_unique_communal_id already checks zones+owners; still skip
        # collisions against this owner's pending row if any.
        if owner_communal_id_taken(db, candidate):
            continue
        owner.communal_id = candidate
        db.flush()
        return candidate

    fallback = f"COMM-{secrets.token_hex(4).upper()}"
    owner.communal_id = fallback
    db.flush()
    return fallback


def assert_may_generate_communal_id(owner) -> None:
    """Individuals cannot mint new Communal IDs — only admins of other tiers."""
    from fastapi import HTTPException, status

    from app.services.account_type_policy import is_individual_account_type

    if is_individual_account_type(getattr(owner, "account_type", None)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Individual accounts cannot generate Communal IDs. "
                "Use the Communal ID assigned to your account."
            ),
        )


def resolve_communal_id_for_owner(owner, requested: str | None) -> str:
    """Return the Communal ID an owner may apply to zones.

    Individuals are locked to their assigned ID. Other tiers may use the
    requested value (validated by the caller).
    """
    from fastapi import HTTPException, status

    from app.services.account_type_policy import is_individual_account_type

    assigned = normalize_reference_id(getattr(owner, "communal_id", None) or "")
    requested_norm = normalize_reference_id(requested or "")

    if is_individual_account_type(getattr(owner, "account_type", None)):
        if not assigned:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Individual account is missing an assigned Communal ID.",
            )
        if requested_norm and requested_norm != assigned:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Individual accounts can only use their assigned Communal ID "
                    f"({assigned})."
                ),
            )
        return assigned

    return requested_norm


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
