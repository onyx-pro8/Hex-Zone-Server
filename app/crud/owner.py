"""CRUD operations for Owner/User."""
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.future import select
from sqlalchemy import func
from app.models import Owner, Zone
from app.models.owner import AccountType, OwnerRole
from app.schemas.schemas import OwnerCreate, OwnerUpdate
from app.core.security import get_password_hash, generate_api_key
from app.crud.zone import apply_zone_geo_fence_geojson
from typing import Optional


def create_owner(
    db: Session,
    owner: OwnerCreate,
    *,
    api_key: str | None = None,
    communal_id: str | None = None,
) -> Owner:
    """Create a new owner.

    ``communal_id`` may be a pre-issued ID from a member-invite QR. When set,
    Individual accounts keep that value instead of minting a new one.
    """
    api_key = api_key or generate_api_key()
    tier_level = getattr(owner, "tier_level", None)
    account_key = (
        owner.account_type.value
        if hasattr(owner.account_type, "value")
        else str(owner.account_type)
    ).strip().lower()
    if account_key == "enhanced_plus":
        tier_level = int(tier_level) if tier_level is not None else 1
    else:
        tier_level = None
    from app.services.communal_zone_service import normalize_reference_id

    reserved = normalize_reference_id(communal_id or "") or None
    db_owner = Owner(
        email=owner.email,
        zone_id=owner.zone_id,
        first_name=owner.first_name,
        last_name=owner.last_name,
        account_type=owner.account_type,
        tier_level=tier_level,
        role=owner.role,
        account_owner_id=owner.account_owner_id,
        hashed_password=get_password_hash(owner.password),
        api_key=api_key,
        phone=owner.phone,
        address=owner.address,
        communal_id=reserved,
    )
    db.add(db_owner)
    db.flush()
    # Account roots (admins and Individual user-only accounts) point at themselves.
    if db_owner.account_owner_id is None:
        db_owner.account_owner_id = db_owner.id
        db.flush()
    # Individual accounts always receive a unique server-assigned Communal ID.
    from app.services.communal_zone_service import assign_owner_communal_id

    assign_owner_communal_id(db, db_owner)
    db.refresh(db_owner)
    return db_owner


def get_owner(db: Session, owner_id: int) -> Optional[Owner]:
    """Get an owner by ID."""
    result = db.execute(
        select(Owner)
        .where(Owner.id == owner_id)
        .options(selectinload(Owner.devices))
    )
    owner = result.scalars().first()

    if not owner:
        return None

    if db.bind and db.bind.dialect.name == "sqlite":
        # Avoid GeoAlchemy / SpatiaLite reads on in-memory SQLite (tests); callers use **owner.zone_id**.
        return owner

    zone_rows = db.execute(
        select(Zone, func.ST_AsGeoJSON(Zone.geo_fence_polygon).label("geo_fence_polygon"))
        .where(Zone.owner_id == owner_id)
    ).all()

    zones = []
    for zone, geojson_text in zone_rows:
        apply_zone_geo_fence_geojson(zone, geojson_text)
        zones.append(zone)

    owner.zones = zones
    return owner


def get_owner_by_email(db: Session, email: str) -> Optional[Owner]:
    """Get an owner by email (case-insensitive)."""
    normalized = email.strip().lower()
    result = db.execute(select(Owner).where(func.lower(Owner.email) == normalized))
    return result.scalars().first()


def get_owner_by_api_key(db: Session, api_key: str) -> Optional[Owner]:
    """Get an owner by API key."""
    result = db.execute(select(Owner).where(Owner.api_key == api_key))
    return result.scalars().first()


def list_owners(db: Session, skip: int = 0, limit: int = 100):
    """List all owners."""
    result = db.execute(
        select(Owner)
        .offset(skip)
        .limit(limit)
        .options(selectinload(Owner.devices), selectinload(Owner.zones))
    )
    return result.scalars().all()


def cascade_account_type_from_administrator(
    db: Session,
    administrator: Owner,
    account_type: AccountType,
) -> None:
    """Keep invited users on Individual (Exclusive) under this administrator."""
    if administrator.role.value != "administrator":
        return
    from app.services.account_type_policy import account_type_for_invited_member
    from app.services.communal_zone_service import assign_owner_communal_id

    member_type = account_type_for_invited_member(administrator)
    root_id = administrator.account_owner_id or administrator.id
    members = (
        db.query(Owner)
        .filter(
            Owner.account_owner_id == root_id,
            Owner.id != administrator.id,
        )
        .all()
    )
    for member in members:
        member.account_type = member_type
        assign_owner_communal_id(db, member)


def update_owner(db: Session, owner_id: int, owner_update: OwnerUpdate) -> Optional[Owner]:
    """Update an owner."""
    db_owner = get_owner(db, owner_id)
    if not db_owner:
        return None
    
    update_data = owner_update.model_dump(exclude_unset=True)
    new_account_type = update_data.pop("account_type", None)
    new_tier_level = update_data.pop("tier_level", None)
    new_role = update_data.pop("role", None)
    if "email" in update_data and isinstance(update_data["email"], str):
        update_data["email"] = update_data["email"].strip().lower()
    if "avatar_url" in update_data:
        raw_avatar = update_data["avatar_url"]
        if raw_avatar is None or str(raw_avatar).strip() == "":
            update_data["avatar_url"] = None
        else:
            value = str(raw_avatar).strip()
            # Clients receive thin /owners/{id}/avatar URLs for display. Never
            # persist those back — they would replace the real Catbox/data URL.
            path = value.split("?", 1)[0].rstrip("/")
            if path.endswith("/avatar") and "/owners/" in path:
                update_data.pop("avatar_url")
            else:
                update_data["avatar_url"] = value
    for field, value in update_data.items():
        setattr(db_owner, field, value)

    if new_role is not None:
        role_value = new_role.value if hasattr(new_role, "value") else str(new_role)
        db_owner.role = OwnerRole(role_value)
        if db_owner.role == OwnerRole.ADMINISTRATOR and db_owner.account_owner_id is None:
            db_owner.account_owner_id = db_owner.id

    if new_account_type is not None:
        account_type_value = (
            new_account_type.value
            if hasattr(new_account_type, "value")
            else str(new_account_type)
        )
        db_owner.account_type = AccountType(account_type_value)
        cascade_account_type_from_administrator(db, db_owner, db_owner.account_type)
        from app.services.communal_zone_service import assign_owner_communal_id

        assign_owner_communal_id(db, db_owner)

    # Keep Organization capacity level in sync with account type.
    effective_type = str(db_owner.account_type.value).strip().lower()
    if effective_type == "enhanced_plus":
        if new_tier_level is not None:
            db_owner.tier_level = int(new_tier_level)
        elif getattr(db_owner, "tier_level", None) is None:
            db_owner.tier_level = 1
    elif new_account_type is not None or new_tier_level is not None:
        db_owner.tier_level = None
    
    db.flush()
    db.refresh(db_owner)
    return db_owner


def delete_owner(db: Session, owner_id: int) -> bool:
    """Delete an owner."""
    db_owner = get_owner(db, owner_id)
    if not db_owner:
        return False
    
    db.delete(db_owner)
    return True


def count_owners(db: Session) -> int:
    """Count all owners."""
    result = db.execute(select(Owner))
    return len(result.scalars().all())
