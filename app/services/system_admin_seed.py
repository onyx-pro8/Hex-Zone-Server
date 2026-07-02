"""Bootstrap the built-in system administrator (Private tier)."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.security import generate_api_key, get_password_hash
from app.crud import owner as owner_crud
from app.models.owner import AccountType, Owner, OwnerRole

logger = logging.getLogger(__name__)

SYSTEM_ADMIN_EMAIL = "admin@test.com"
SYSTEM_ADMIN_PASSWORD = "123456"
SYSTEM_ADMIN_FIRST_NAME = "System"
SYSTEM_ADMIN_LAST_NAME = "Admin"
SYSTEM_ADMIN_ZONE_ID = "DISTRICT-11"


def ensure_system_admin(db: Session) -> Owner | None:
    """Create the default Private system administrator when missing."""
    existing = owner_crud.get_owner_by_email(db, SYSTEM_ADMIN_EMAIL)
    if existing:
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
    db.flush()
    db.commit()
    db.refresh(owner)
    logger.info(
        "Seeded system administrator %s (account_type=private, zone_id=%s)",
        SYSTEM_ADMIN_EMAIL,
        SYSTEM_ADMIN_ZONE_ID,
    )
    return owner
