"""Account visibility and ownership rules."""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Owner
from app.models.owner import OwnerRole
from app.services.account_type_policy import is_system_administrator
from app.services.device_entitlements import (
    account_type_supports_member_invite,
    admin_user_members_at_capacity,
    assert_account_allows_user_members,
    assert_admin_user_member_capacity,
)


def _all_owner_ids(db: Session, *, include_inactive: bool = False) -> list[int]:
    """Return every owner id (platform-wide)."""
    query = db.query(Owner.id)
    if not include_inactive:
        query = query.filter(Owner.active.is_(True))
    return [row[0] for row in query.all()]


def account_root_id(owner: Owner) -> int:
    """Return the account holder id for an owner."""
    return owner.account_owner_id or owner.id


NETWORK_ID_REQUIRED_DETAIL = (
    "User registration requires the Network ID of the administrator."
)
NETWORK_ADMIN_NOT_FOUND_DETAIL = "No administrator found for this Network ID."
AMBIGUOUS_NETWORK_ADMIN_DETAIL = (
    "Several administrators share this Network ID. Enter the administrator email."
)
NETWORK_ADMIN_EMAIL_NOT_FOUND_DETAIL = (
    "No administrator with that email uses this Network ID."
)


def _accept_account_owner(
    db: Session,
    account_owner: Owner | None,
    *,
    account_type: str,
    enforce_account_type: bool = True,
) -> int:
    """Confirm ``account_owner`` can accept a linked user.

    Network-name signup identifies the administrator without the plan card the
    person selected, so ``enforce_account_type`` is false on that path. The
    caller then stores the invited-member type taken from the administrator.
    """
    if not account_owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account owner not found")
    if str(account_owner.role.value) != "administrator":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="account_owner_id must reference an administrator",
        )
    assert_account_allows_user_members(account_owner.account_type.value)
    requested = str(account_type).strip().lower()
    if (
        enforce_account_type
        and requested != "exclusive"
        and str(account_owner.account_type.value) != requested
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="account_owner_id account type mismatch",
        )
    assert_admin_user_member_capacity(db, account_owner)
    return account_owner.id


def _administrators_for_network(db: Session, zone_id: str) -> list[Owner]:
    """Active administrators whose Network ID matches ``zone_id``."""
    network = (zone_id or "").strip().lower()
    if not network:
        return []
    rows = (
        db.query(Owner)
        .filter(
            func.lower(func.trim(Owner.zone_id)) == network,
            Owner.role == OwnerRole.ADMINISTRATOR,
            Owner.active.is_(True),
        )
        .order_by(Owner.id.asc())
        .all()
    )
    # Private system administrators are not a joinable network.
    return [admin for admin in rows if not is_system_administrator(admin)]


def resolve_account_owner_id(
    db: Session,
    *,
    role: str,
    requested_account_owner_id: int | None,
    zone_id: str,
    account_type: str,
    administrator_email: str | None = None,
) -> int | None:
    """Resolve account owner linkage for new owner registrations.

    User signups may omit the numeric account owner id. The Network ID
    (``zone_id``) selects the administrator. The plan card on the signup form
    is not used for that lookup. When more than one administrator shares the
    Network ID, ``administrator_email`` picks one.
    """
    if role == "administrator":
        return None

    # Solo Individual (exclusive) registration — own account root, no inviter.
    if (
        str(account_type).strip().lower() == "exclusive"
        and requested_account_owner_id is None
    ):
        return None

    if requested_account_owner_id is not None:
        return _accept_account_owner(
            db,
            db.get(Owner, requested_account_owner_id),
            account_type=account_type,
        )

    network = (zone_id or "").strip()
    if not network:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=NETWORK_ID_REQUIRED_DETAIL,
        )

    requested = str(account_type).strip().lower()
    admins = [
        admin
        for admin in _administrators_for_network(db, network)
        if str(admin.account_type.value) == requested
    ]
    email = (administrator_email or "").strip().lower()

    if email:
        matched = [admin for admin in admins if (admin.email or "").strip().lower() == email]
        if not matched:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=NETWORK_ADMIN_EMAIL_NOT_FOUND_DETAIL,
            )
        return _accept_account_owner(
            db,
            matched[0],
            account_type=account_type,
            enforce_account_type=False,
        )

    if len(admins) == 1:
        return _accept_account_owner(
            db,
            admins[0],
            account_type=account_type,
            enforce_account_type=False,
        )
    if len(admins) > 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=AMBIGUOUS_NETWORK_ADMIN_DETAIL,
        )
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=NETWORK_ADMIN_NOT_FOUND_DETAIL,
    )


_JOINABLE_NETWORK_LABELS = {
    "private_plus": "Family",
    "enhanced": "Individual Pro",
    "enhanced_plus": "Organization",
    "exclusive": "Individual",
    "private": "Private",
}


def list_joinable_networks(db: Session, *, account_type: str | None = None) -> list[dict]:
    """Active networks a new user can join, one row per Network ID.

    When ``account_type`` is set, only administrators of that same type are
    included. System-administrator networks and full accounts are omitted.
    Email addresses are not included.
    """
    requested = (account_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    admins = (
        db.query(Owner)
        .filter(Owner.role == OwnerRole.ADMINISTRATOR, Owner.active.is_(True))
        .order_by(Owner.id.asc())
        .all()
    )
    grouped: dict[str, dict] = {}
    for admin in admins:
        if is_system_administrator(admin):
            continue
        admin_type = str(admin.account_type.value)
        if requested and admin_type != requested:
            continue
        if not account_type_supports_member_invite(admin_type):
            continue
        if admin_user_members_at_capacity(db, admin):
            continue
        network_id = (admin.zone_id or "").strip()
        if not network_id:
            continue
        key = network_id.lower()
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {"network_id": network_id, "account_types": [], "count": 0}
            grouped[key] = bucket
        bucket["count"] += 1
        if admin_type not in bucket["account_types"]:
            bucket["account_types"].append(admin_type)

    rows: list[dict] = []
    for bucket in grouped.values():
        types: list[str] = bucket["account_types"]
        labels = [_JOINABLE_NETWORK_LABELS.get(item, item) for item in types]
        rows.append(
            {
                "network_id": bucket["network_id"],
                "account_type": types[0] if len(types) == 1 else "",
                "label": " · ".join(labels),
                "administrator_count": bucket["count"],
            }
        )
    rows.sort(key=lambda row: str(row["network_id"]).lower())
    return rows


def visible_owner_ids(db: Session, owner: Owner, include_inactive: bool = False) -> list[int]:
    """Return owners visible to caller based on role/account type rules."""
    if is_system_administrator(owner):
        return _all_owner_ids(db, include_inactive=include_inactive)

    # Default-deny for non-admin callers: only explicit administrators can see account-wide owners.
    if owner.role.value != "administrator":
        return [owner.id]

    root_id = account_root_id(owner)
    query = db.query(Owner.id).filter(Owner.account_owner_id == root_id)
    if not include_inactive:
        query = query.filter(Owner.active.is_(True))
    rows = query.all()
    owner_ids = [row[0] for row in rows]
    if owner.id not in owner_ids:
        owner_ids.append(owner.id)
    return owner_ids


def account_propagation_owner_ids(
    db: Session,
    owner: Owner,
    *,
    include_inactive: bool = False,
) -> list[int]:
    """All owners on the same account root — used for alarm/alert fan-out.

    Unlike ``visible_owner_ids``, non-admin senders still reach every active
    member on their account (admin, invited users, devices).
    """
    root_id = account_root_id(owner)
    query = db.query(Owner.id).filter((Owner.id == root_id) | (Owner.account_owner_id == root_id))
    if not include_inactive:
        query = query.filter(Owner.active.is_(True))
    rows = query.all()
    owner_ids = [row[0] for row in rows]
    if owner.id not in owner_ids and (include_inactive or owner.active):
        owner_ids.append(owner.id)
    return owner_ids


def messaging_visible_owner_ids(
    db: Session,
    owner: Owner,
    *,
    include_inactive: bool = False,
    require_same_zone: bool = True,
) -> list[int]:
    """Return owner ids visible for private-message receiver discovery."""
    if is_system_administrator(owner):
        return _all_owner_ids(db, include_inactive=include_inactive)

    root_id = account_root_id(owner)
    query = db.query(Owner.id).filter((Owner.id == root_id) | (Owner.account_owner_id == root_id))
    if not include_inactive:
        query = query.filter(Owner.active.is_(True))
    if require_same_zone:
        query = query.filter(Owner.zone_id == owner.zone_id)
    rows = query.all()
    owner_ids = [row[0] for row in rows]
    if owner.id not in owner_ids and (include_inactive or owner.active):
        owner_ids.append(owner.id)
    return owner_ids


def can_message_owner(sender: Owner, receiver: Owner, *, require_same_zone: bool = True) -> bool:
    """Check whether sender can message receiver under account/zone policy."""
    if not receiver.active:
        return False
    if sender.id == receiver.id:
        return False
    if account_root_id(sender) != account_root_id(receiver):
        return False
    if require_same_zone and sender.zone_id != receiver.zone_id:
        return False
    return True


def zone_listing_owner_ids(db: Session, owner: Owner) -> list[int]:
    """Return owner ids whose zone *records* may appear in a listing query.

    System administrators see every zone on the platform.
    Account administrators and members still receive account owner ids so primary
    zones (owned by the admin) can be loaded; secondary visibility is filtered
    afterward to creator-only via ``is_primary`` / ``creator_id``.
    """
    if is_system_administrator(owner):
        return _all_owner_ids(db, include_inactive=True)

    if owner.role.value != "administrator":
        root_id = account_root_id(owner)
        if root_id == owner.id:
            return [owner.id]
        return [owner.id, root_id]

    return visible_owner_ids(db, owner)


def visible_zone_owner_ids(db: Session, owner: Owner) -> list[int]:
    """Deprecated alias: use zone_listing_owner_ids."""
    return zone_listing_owner_ids(db, owner)

