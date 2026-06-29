"""Authentication business logic for contract endpoints."""
import logging
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import create_access_token, generate_api_key, get_password_hash, verify_password
from app.models import Owner
from app.models.owner import OwnerRole
from app.services.access_policy import resolve_account_owner_id
from app.services.geocoding_service import geocode_address
from app.services.registration_code_service import require_and_consume_admin_registration_code

logger = logging.getLogger(__name__)


def _try_geocode_owner_address(owner: Owner) -> None:
    """Best-effort populate `owner.latitude/longitude` from `owner.address`.

    Used at registration time and as a lazy backfill on `/me`. Network errors
    are swallowed by `geocode_address`; this helper only mutates the owner row
    when coordinates were resolved.
    """
    if owner.latitude is not None and owner.longitude is not None:
        return
    coords = geocode_address(owner.address)
    if coords is None:
        return
    lat, lng = coords
    owner.latitude = lat
    owner.longitude = lng
    owner.location_updated_at = datetime.utcnow()
    logger.info(
        "Geocoded owner %s address %r to (%.6f, %.6f)",
        owner.id,
        owner.address,
        lat,
        lng,
    )


def _to_contract_account_type(account_type: str) -> str:
    normalized = str(account_type).strip().lower()
    mapping = {
        "private": "PRIVATE",
        "private_plus": "PRIVATE_PLUS",
        "exclusive": "EXCLUSIVE",
        "enhanced": "ENHANCED",
        "enhanced_plus": "ENHANCED_PLUS",
    }
    contract_type = mapping.get(normalized)
    if contract_type is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported account type: {account_type}",
        )
    return contract_type


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if not parts:
        return "User", "User"
    if len(parts) == 1:
        return parts[0], "User"
    return parts[0], " ".join(parts[1:])


def _to_owner_role(registration_type: str | None) -> OwnerRole:
    normalized = str(registration_type or "ADMINISTRATOR").strip().upper().replace("-", "_").replace(" ", "_")
    if normalized in {"ADMIN", "ADMINISTRATOR"}:
        return OwnerRole.ADMINISTRATOR
    if normalized == "USER":
        return OwnerRole.USER
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Unsupported registration type: {registration_type}",
    )


def _get_map_center(db: Session, owner_id: int) -> dict | None:
    """Return the owner's canonical map center.

    Reads directly from `owners.latitude / owners.longitude`, which is the
    single source of truth for the user's last known location (kept in sync by
    `upsert_member_location` and the startup backfill). Returns ``None`` when
    the owner has never published a location.
    """
    owner = db.get(Owner, owner_id)
    if owner is None:
        return None
    if owner.latitude is None or owner.longitude is None:
        return None
    return {"latitude": owner.latitude, "longitude": owner.longitude}


def register_user(db: Session, payload: dict) -> dict:
    existing = db.query(Owner).filter(Owner.email == payload["email"]).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    first_name, last_name = _split_name(payload["name"])
    account_type_value = _to_contract_account_type(payload["accountType"]).lower()
    role_value = _to_owner_role(payload.get("registrationType"))
    preallocated_api_key: str | None = None
    if role_value == OwnerRole.ADMINISTRATOR:
        preallocated_api_key = require_and_consume_admin_registration_code(
            db,
            payload.get("registrationCode"),
            registration_email=payload.get("email"),
            account_type=account_type_value,
        )
    account_owner_id = resolve_account_owner_id(
        db,
        role=role_value.value,
        requested_account_owner_id=payload.get("accountOwnerId"),
        zone_id=payload.get("zoneId") or f"user-{payload['email']}",
        account_type=account_type_value,
    )
    owner = Owner(
        email=payload["email"],
        zone_id=payload.get("zoneId") or f"user-{payload['email']}",
        first_name=first_name,
        last_name=last_name,
        account_type=account_type_value,
        role=role_value,
        account_owner_id=account_owner_id,
        hashed_password=get_password_hash(payload["password"]),
        api_key=preallocated_api_key or generate_api_key(),
        address=payload.get("address", "N/A"),
    )
    db.add(owner)
    db.flush()
    if owner.role.value == "administrator" and owner.account_owner_id is None:
        owner.account_owner_id = owner.id
        db.flush()
    # Best-effort geocode of the wizard address into owners.latitude/longitude so
    # downstream consumers (`/me`, dynamic-zone resolver) have a sane starting
    # map_center even before the device pushes a live position.
    try:
        _try_geocode_owner_address(owner)
        db.flush()
    except Exception as exc:  # pragma: no cover - never block registration
        logger.warning("Address geocoding failed for owner %s: %s", owner.id, exc)
    db.refresh(owner)
    return {
        "id": str(owner.id),
        "email": owner.email,
        "zone_id": owner.zone_id,
        "first_name": owner.first_name,
        "last_name": owner.last_name,
        "account_type": owner.account_type.value,
        "role": owner.role.value,
        "account_owner_id": owner.account_owner_id or owner.id,
        "address": owner.address,
        "phone": owner.phone,
        "active": owner.active,
        "expired": owner.expired,
        "created_at": owner.created_at,
        "updated_at": owner.updated_at,
        "api_key": owner.api_key,
        "mapCenter": _get_map_center(db, owner.id),
    }


def login_user(db: Session, email: str, password: str) -> dict:
    owner = db.query(Owner).filter(Owner.email == email).first()
    if not owner or not verify_password(password, owner.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not owner.active or owner.expired:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive or expired",
        )

    # Lazy backfill: existing owners created before the geocode-at-registration
    # hook still have NULL coordinates. Resolve them once on login so the
    # mobile/web clients receive a populated `mapCenter` immediately.
    if owner.latitude is None or owner.longitude is None:
        try:
            _try_geocode_owner_address(owner)
            db.flush()
        except Exception as exc:  # pragma: no cover - never block login
            logger.warning("Address geocoding failed for owner %s: %s", owner.id, exc)

    token = create_access_token({"sub": str(owner.id)})
    return {
        "token": token,
        "user": {
            "id": str(owner.id),
            "name": f"{owner.first_name} {owner.last_name}".strip(),
            "accountType": _to_contract_account_type(owner.account_type.value),
            "registrationType": owner.role.value.upper(),
            "accountOwnerId": owner.account_owner_id or owner.id,
            "mapCenter": _get_map_center(db, owner.id),
        },
    }
