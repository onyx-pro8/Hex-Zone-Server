"""Communal ID registry, multi-ID tagging, and cross-network sharing helpers.

Communal IDs are public codes minted by network administrators. Primary zones
may attach one or more IDs. Zones tagged with an ID become visible to every
member of the ID creator's network without counting toward that network's
zone quota.

The Communal zone type itself does not draw geometry — it only validates and
generates IDs.
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


def _coerce_id_list(raw: Any) -> list[str]:
    if isinstance(raw, str) and raw.strip():
        return [normalize_reference_id(raw)]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        normalized = normalize_reference_id(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def get_communal_ids(zone: Zone) -> list[str]:
    """Return all Communal IDs attached to a zone (multi + legacy single)."""
    config = zone_config(zone)
    ids = _coerce_id_list(config.get("communal_ids") or config.get("communalIds"))
    legacy = config.get("communal_id") or config.get("communalId")
    if isinstance(legacy, str) and legacy.strip():
        normalized = normalize_reference_id(legacy)
        if normalized and normalized not in ids:
            ids.append(normalized)
    return ids


def get_communal_id(zone: Zone) -> Optional[str]:
    """Primary/legacy single Communal ID (first of multi, if any)."""
    ids = get_communal_ids(zone)
    return ids[0] if ids else None


def zone_has_communal_id(zone: Zone, reference_id: str) -> bool:
    normalized = normalize_reference_id(reference_id)
    if not normalized:
        return False
    return normalized in get_communal_ids(zone)


def is_defining_zone(zone: Zone) -> bool:
    contract = zone_contract_type(zone)
    if contract in {"communal_id", "custom_1"}:
        return False
    if contract in DEFINING_ZONE_TYPES:
        return True
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


def extract_communal_ids_from_config(config: dict[str, Any] | None) -> list[str]:
    """Normalize communal_ids / communal_id from a create/update config dict."""
    if not isinstance(config, dict):
        return []
    ids = _coerce_id_list(config.get("communal_ids") or config.get("communalIds"))
    legacy = config.get("communal_id") or config.get("communalId")
    if isinstance(legacy, str) and legacy.strip():
        normalized = normalize_reference_id(legacy)
        if normalized and normalized not in ids:
            ids.append(normalized)
    return [cid for cid in ids if is_valid_reference_format(cid)]


def apply_communal_ids_to_config(
    config: dict[str, Any],
    communal_ids: list[str],
) -> dict[str, Any]:
    """Write multi + legacy single fields; clear when empty."""
    out = dict(config)
    out.pop("communalId", None)
    out.pop("communalIds", None)
    cleaned = [
        normalize_reference_id(cid)
        for cid in communal_ids
        if is_valid_reference_format(cid)
    ]
    # Dedupe preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for cid in cleaned:
        if cid in seen:
            continue
        seen.add(cid)
        unique.append(cid)
    if unique:
        out["communal_ids"] = unique
        out["communal_id"] = unique[0]
    else:
        out.pop("communal_ids", None)
        out.pop("communal_id", None)
    return out


def registry_communal_id_taken(db: Session, reference_id: str) -> bool:
    from app.models import CommunalIdRegistry

    normalized = normalize_reference_id(reference_id)
    if not normalized or db is None:
        return False
    return (
        db.query(CommunalIdRegistry.id)
        .filter(CommunalIdRegistry.reference_id == normalized)
        .first()
        is not None
    )


def get_registry_entry(db: Session, reference_id: str):
    from app.models import CommunalIdRegistry

    normalized = normalize_reference_id(reference_id)
    if not normalized or db is None:
        return None
    return (
        db.query(CommunalIdRegistry)
        .filter(CommunalIdRegistry.reference_id == normalized)
        .first()
    )


def list_registry_ids_for_network(db: Session, network_id: str) -> list[str]:
    from app.models import CommunalIdRegistry

    network = str(network_id or "").strip()
    if not network or db is None:
        return []
    rows = (
        db.query(CommunalIdRegistry.reference_id)
        .filter(CommunalIdRegistry.network_id == network)
        .all()
    )
    return [normalize_reference_id(row[0]) for row in rows if row and row[0]]


def list_public_communal_ids(db: Session, owner=None) -> list[dict[str, Any]]:
    """All public Communal IDs in the registry (any network), including zero-zone IDs.

    Communal IDs are global — any network admin may attach any registered ID
    to a primary zone. ``owner`` is unused but kept for call-site compatibility.
    """
    from app.models import CommunalIdRegistry, Owner

    if db is None:
        return []
    rows = (
        db.query(CommunalIdRegistry)
        .order_by(CommunalIdRegistry.created_at.desc(), CommunalIdRegistry.id.desc())
        .all()
    )
    creator_ids = {
        int(getattr(row, "creator_id", 0) or 0)
        for row in rows
        if getattr(row, "creator_id", None)
    }
    creators: dict[int, Owner] = {}
    if creator_ids:
        for person in db.query(Owner).filter(Owner.id.in_(tuple(creator_ids))).all():
            creators[int(person.id)] = person

    out: list[dict[str, Any]] = []
    for row in rows:
        reference_id = normalize_reference_id(getattr(row, "reference_id", "") or "")
        if not reference_id:
            continue
        matched = find_zones_by_communal_id(db, reference_id)
        creator_id = int(getattr(row, "creator_id", 0) or 0) or None
        person = creators.get(int(creator_id)) if creator_id else None
        if person is not None:
            first = str(getattr(person, "first_name", "") or "").strip()
            last = str(getattr(person, "last_name", "") or "").strip()
            creator_name = f"{first} {last}".strip() or None
        else:
            creator_name = None
        out.append(
            {
                "reference_id": reference_id,
                "creator_id": creator_id,
                "creator_name": creator_name,
                "network_id": str(getattr(row, "network_id", "") or "").strip(),
                "zone_count": len(matched),
                "created_at": (
                    row.created_at.isoformat() if getattr(row, "created_at", None) else None
                ),
            }
        )
    return out


# Back-compat alias used by older call sites / tests.
def list_network_communal_ids(db: Session, owner) -> list[dict[str, Any]]:
    return list_public_communal_ids(db, owner)


def zone_communal_id_taken(db: Session, reference_id: str) -> bool:
    """True when any active zone config already stores this Communal ID."""
    normalized = normalize_reference_id(reference_id)
    if not normalized or db is None:
        return False
    rows = (
        db.query(Zone.id, Zone.parameters)
        .filter(Zone.active.is_(True))
        .all()
    )
    for _, parameters in rows:
        params = parameters if isinstance(parameters, dict) else {}
        config = params.get("config") if isinstance(params.get("config"), dict) else {}
        ids = _coerce_id_list(config.get("communal_ids") or config.get("communalIds"))
        legacy = config.get("communal_id") or config.get("communalId")
        if isinstance(legacy, str) and legacy.strip():
            ids.append(normalize_reference_id(legacy))
        if normalized in ids:
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
        ids = _coerce_id_list(config.get("communal_ids") or config.get("communalIds"))
        legacy = config.get("communal_id") or config.get("communalId")
        if isinstance(legacy, str) and legacy.strip():
            ids.append(normalize_reference_id(legacy))
        if normalized in ids:
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
        return []


def find_zones_by_communal_ids(db: Session, reference_ids: list[str]) -> list[Zone]:
    wanted = {
        normalize_reference_id(cid)
        for cid in reference_ids
        if is_valid_reference_format(cid)
    }
    if not wanted or db is None:
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
        ids = set(_coerce_id_list(config.get("communal_ids") or config.get("communalIds")))
        legacy = config.get("communal_id") or config.get("communalId")
        if isinstance(legacy, str) and legacy.strip():
            ids.add(normalize_reference_id(legacy))
        if ids & wanted:
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
        return []


def list_zones_shared_into_network(db: Session, owner) -> list[Zone]:
    """Zones tagged with Communal IDs minted by this network's admins.

    Visible to all network members; callers should not count these toward quota.
    """
    network = caller_network_id(owner)
    if not network:
        return []
    registry_ids = list_registry_ids_for_network(db, network)
    if not registry_ids:
        return []
    return find_zones_by_communal_ids(db, registry_ids)


def owner_communal_id_taken(db: Session, reference_id: str) -> bool:
    """True when another owner already holds this legacy assigned Communal ID."""
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
    """True when a member-invite QR already reserved this Communal ID (legacy)."""
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
    """True when the ID is registered, used on a zone, or held on legacy owner/QR."""
    return (
        registry_communal_id_taken(db, reference_id)
        or zone_communal_id_taken(db, reference_id)
        or owner_communal_id_taken(db, reference_id)
        or qr_invite_communal_id_taken(db, reference_id)
    )


def mint_invite_communal_id(db: Session) -> str:
    """Deprecated — invites no longer mint Communal IDs. Returns empty string."""
    del db
    return ""


def zone_network_id(zone: Zone) -> str:
    return str(getattr(zone, "zone_id", None) or "").strip()


def caller_network_id(owner) -> str:
    return str(getattr(owner, "zone_id", None) or "").strip()


def zone_eligible_for_communal_assignment(
    owner,
    zone: Zone,
    *,
    account_owner_ids: list[int] | None = None,
) -> bool:
    """True when the caller may attach a Communal ID to this defining zone.

    Only network / system administrators may stamp IDs, and only onto **primary**
    defining zones. System administrators may use any public defining primary.
    """
    from app.services.account_type_policy import is_system_administrator
    from app.services.zone_policy import zone_is_primary
    from app.models.owner import OwnerRole

    if not is_zone_public(zone):
        return False
    if not zone_is_primary(zone):
        return False

    role = getattr(getattr(owner, "role", None), "value", None) or str(
        getattr(owner, "role", "") or ""
    )
    if is_system_administrator(owner):
        return True
    if str(role).strip().lower() != OwnerRole.ADMINISTRATOR.value:
        return False

    if account_owner_ids is not None:
        allowed = {int(oid) for oid in account_owner_ids}
        return int(getattr(zone, "owner_id", 0) or 0) in allowed

    network = caller_network_id(owner)
    if not network or zone_network_id(zone) != network:
        return False
    return True


def list_public_defining_zones(
    db: Session,
    *,
    owner=None,
    skip: int = 0,
    limit: int = 200,
) -> list[Zone]:
    """List primary defining zones in the caller's account (legacy picker)."""
    if db is None:
        return []

    from app.services.account_type_policy import is_system_administrator
    from app.services.access_policy import zone_listing_owner_ids

    query = db.query(Zone).filter(Zone.active.is_(True))
    account_owner_ids: list[int] | None = None

    if owner is not None and not is_system_administrator(owner):
        account_owner_ids = [int(oid) for oid in zone_listing_owner_ids(db, owner)]
        if not account_owner_ids:
            return []
        query = query.filter(Zone.owner_id.in_(tuple(account_owner_ids)))

    zones = query.order_by(Zone.updated_at.desc(), Zone.id.desc()).all()
    if owner is None:
        public = [z for z in zones if is_zone_public(z)]
    else:
        public = [
            z
            for z in zones
            if zone_eligible_for_communal_assignment(
                owner, z, account_owner_ids=account_owner_ids
            )
        ]
    return public[skip : skip + limit]


def generate_unique_communal_id(db: Session | None = None) -> str:
    for _ in range(32):
        candidate = f"COMM-{secrets.token_hex(3).upper()}"
        if db is not None and communal_id_exists(db, candidate):
            continue
        return candidate
    return f"COMM-{secrets.token_hex(4).upper()}"


def register_communal_id(db: Session, owner, reference_id: str | None = None) -> str:
    """Persist a public Communal ID owned by this admin's network."""
    from app.models import CommunalIdRegistry

    normalized = normalize_reference_id(reference_id or "")
    if not normalized:
        normalized = generate_unique_communal_id(db)
    if not is_valid_reference_format(normalized):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Communal ID must be 3–32 characters (letters, numbers, hyphen, underscore).",
        )
    if communal_id_exists(db, normalized):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Communal ID {normalized} already exists.",
        )

    network = caller_network_id(owner)
    if not network:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Administrator network id is required to mint a Communal ID.",
        )

    row = CommunalIdRegistry(
        reference_id=normalized,
        creator_id=int(owner.id),
        network_id=network,
    )
    db.add(row)
    db.flush()
    return normalized


def assign_owner_communal_id(db: Session, owner) -> str:
    """No-op: Individuals/members are no longer issued Communal IDs.

    Returns any legacy value already stored on the owner, otherwise "".
    """
    del db
    return normalize_reference_id(getattr(owner, "communal_id", None) or "")


def assert_may_generate_communal_id(owner) -> None:
    """Only network / system administrators may mint Communal IDs."""
    from fastapi import HTTPException, status

    from app.models.owner import OwnerRole
    from app.services.account_type_policy import is_individual_account_type

    role = getattr(getattr(owner, "role", None), "value", None) or str(
        getattr(owner, "role", "") or ""
    )
    if str(role).strip().lower() != OwnerRole.ADMINISTRATOR.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only network administrators can generate Communal IDs.",
        )
    if is_individual_account_type(getattr(owner, "account_type", None)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Individual accounts cannot generate Communal IDs.",
        )


def assert_may_use_communal_tools(owner) -> None:
    """Members and Individuals cannot use Communal validate/generate tools."""
    assert_may_generate_communal_id(owner)


def resolve_communal_id_for_owner(owner, requested: str | None) -> str:
    """Return a requested Communal ID for admins; Individuals may not attach IDs."""
    from fastapi import HTTPException, status

    from app.models.owner import OwnerRole
    from app.services.account_type_policy import is_individual_account_type

    requested_norm = normalize_reference_id(requested or "")

    if is_individual_account_type(getattr(owner, "account_type", None)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Individual accounts cannot attach Communal IDs to zones.",
        )

    role = getattr(getattr(owner, "role", None), "value", None) or str(
        getattr(owner, "role", "") or ""
    )
    if str(role).strip().lower() != OwnerRole.ADMINISTRATOR.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only network administrators can attach Communal IDs to zones.",
        )

    return requested_norm


def assert_communal_ids_attachable(
    db: Session,
    owner,
    communal_ids: list[str],
    *,
    is_primary: bool,
) -> list[str]:
    """Validate that primary-zone Communal IDs exist in the public registry."""
    from fastapi import HTTPException, status

    cleaned = [
        normalize_reference_id(cid)
        for cid in communal_ids
        if is_valid_reference_format(cid)
    ]
    if not cleaned:
        return []
    if not is_primary:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Communal IDs can only be attached to primary zones.",
        )
    # Ensure caller is an admin (raises for individuals/members).
    resolve_communal_id_for_owner(owner, cleaned[0])

    missing: list[str] = []
    for cid in cleaned:
        if not (
            registry_communal_id_taken(db, cid) or zone_communal_id_taken(db, cid)
        ):
            missing.append(cid)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Unknown Communal ID(s): "
                + ", ".join(missing)
                + ". Validate or generate them first."
            ),
        )
    return cleaned


def assign_communal_id(
    zone: Zone,
    reference_id: str,
    *,
    is_public: bool | None = None,
) -> dict[str, Any]:
    """Add a Communal ID to a zone (keeps existing multi-IDs)."""
    normalized = normalize_reference_id(reference_id)
    params = dict(zone_parameters(zone))
    config = dict(params.get("config") if isinstance(params.get("config"), dict) else {})
    existing = get_communal_ids(zone)
    if normalized not in existing:
        existing.append(normalized)
    config = apply_communal_ids_to_config(config, existing)
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


def assign_communal_ids(
    zone: Zone,
    reference_ids: list[str],
    *,
    is_public: bool | None = None,
) -> dict[str, Any]:
    """Replace the zone's Communal ID list."""
    params = dict(zone_parameters(zone))
    config = dict(params.get("config") if isinstance(params.get("config"), dict) else {})
    config = apply_communal_ids_to_config(config, reference_ids)
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
    """Generate a unique communal ID (no geometry). Does not persist — caller registers."""
    del owner_ids
    reference_id = generate_unique_communal_id(db)
    return {
        "valid": True,
        "zone_type": "communal_id",
        "reference_id": reference_id,
        "display_name": reference_id,
        "geometry": {},
        "config": {"communal_id": reference_id, "communal_ids": [reference_id]},
        "h3_cells": [],
        "source": "generated",
        "exists": True,
        "message": f"Generated new Communal ID {reference_id}.",
        "zones": [],
    }


def resolve_communal_reference(
    db: Session | None,
    owner_ids: list[int] | None,
    reference_id: str,
) -> Optional[dict[str, Any]]:
    """Existence check for a communal ID (registry and/or zones)."""
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
    registered = registry_communal_id_taken(db, normalized)
    exists = bool(matched) or registered
    if matched:
        message = f"Communal ID found on {len(matched)} zone(s)."
    elif registered:
        message = "Communal ID is registered and ready to attach to primary zones."
    else:
        message = "Communal ID not found. You can generate a new one."
    return {
        "valid": exists,
        "zone_type": "communal_id",
        "reference_id": normalized,
        "display_name": matched[0].name if matched else normalized,
        "geometry": {},
        "config": {
            "communal_id": normalized,
            "communal_ids": [normalized],
        },
        "h3_cells": [],
        "source": "database",
        "exists": exists,
        "message": message,
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
