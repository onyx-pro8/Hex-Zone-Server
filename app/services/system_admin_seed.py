"""Bootstrap the built-in system administrator (Private tier)."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.security import generate_api_key, get_password_hash, verify_password
from app.crud import owner as owner_crud
from app.models.owner import AccountType, Owner, OwnerRole

logger = logging.getLogger(__name__)

SYSTEM_ADMIN_EMAIL = "admin@test.com"
SYSTEM_ADMIN_PASSWORD = "123456"
SYSTEM_ADMIN_FIRST_NAME = "System"
SYSTEM_ADMIN_LAST_NAME = "Admin"
SYSTEM_ADMIN_ZONE_ID = "DISTRICT-11"


def _apply_system_admin_defaults(owner: Owner) -> None:
    """Keep the built-in system administrator row aligned with product defaults."""
    owner.first_name = SYSTEM_ADMIN_FIRST_NAME
    owner.last_name = SYSTEM_ADMIN_LAST_NAME
    owner.account_type = AccountType.PRIVATE
    owner.role = OwnerRole.ADMINISTRATOR
    owner.active = True
    owner.expired = False
    if not (owner.zone_id or "").strip():
        owner.zone_id = SYSTEM_ADMIN_ZONE_ID
    if not (owner.address or "").strip():
        owner.address = "System Administrator"
    if not owner.api_key:
        owner.api_key = generate_api_key()
    if not verify_password(SYSTEM_ADMIN_PASSWORD, owner.hashed_password):
        owner.hashed_password = get_password_hash(SYSTEM_ADMIN_PASSWORD)


def ensure_system_admin(db: Session) -> Owner:
    """Create or refresh the default Private system administrator."""
    existing = owner_crud.get_owner_by_email(db, SYSTEM_ADMIN_EMAIL)
    if existing:
        _apply_system_admin_defaults(existing)
        if existing.account_owner_id is None:
            existing.account_owner_id = existing.id
        db.commit()
        db.refresh(existing)
        logger.info("Refreshed system administrator %s", SYSTEM_ADMIN_EMAIL)
        return existing

    owner = Owner(
        email=SYSTEM_ADMIN_EMAIL,
        zone_id=SYSTEM_ADMIN_ZONE_ID,
        first_name=SYSTEM_ADMIN_FIRST_NAME,
        last_name=SYSTEM_ADMIN_LAST_NAME,
        account_type=AccountType.PRIVATE,
        role=OwnerRole.ADMINISTRATOR,
        hashed_password=get_password_hash(SYSTEM_ADMIN_PASSWORD),
        api_key=generate_api_key(),
        address="System Administrator",
        active=True,
        expired=False,
    )
    db.add(owner)
    db.flush()
    owner.account_owner_id = owner.id
    db.commit()
    db.refresh(owner)
    logger.info(
        "Seeded system administrator %s (account_type=private, zone_id=%s)",
        SYSTEM_ADMIN_EMAIL,
        SYSTEM_ADMIN_ZONE_ID,
    )
    return owner
