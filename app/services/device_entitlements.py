"""Account type constraints for devices and member invitations."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.crud import device as device_crud
from app.models import Owner


DEVICE_LIMITS_BY_ACCOUNT_TYPE: dict[str, int | None] = {
    "private": 1,
    "exclusive": 0,  # Individual: smart-home hubs disabled
    "private_plus": 10,
    "enhanced": 1,
    "enhanced_plus": None,
}


def is_client_session_hid(hid: str | None) -> bool:
    """Phone (MOB-) and browser (WEB-) login sessions — not smart-home hubs."""
    normalized = str(hid or "").strip().upper()
    return normalized.startswith(("MOB-", "WEB-"))


def is_smart_home_hid(hid: str | None) -> bool:
    """Dedicated hub / hardware HID used for smart-home integration."""
    normalized = str(hid or "").strip().upper()
    return bool(normalized) and not is_client_session_hid(normalized)


# Max *total* active users (administrator + invited members) per account.
# ``None`` means unlimited; ``1`` means solo (no invited members).
# Organization (enhanced_plus) uses ENHANCED_PLUS_LEVELS via tier_level instead.
USER_MEMBER_LIMITS_BY_ACCOUNT_TYPE: dict[str, int | None] = {
    "private": None,
    "exclusive": 1,
    "private_plus": 10,  # Family
    "enhanced": 2,  # Individual Pro: admin + 1 invited Individual
    "enhanced_plus": None,  # resolved from owner.tier_level
}


def account_type_supports_member_invite(account_type: str) -> bool:
    """Whether administrators of this tier may use member-invite QR."""
    key = str(account_type).strip().lower()
    if key == "enhanced_plus":
        return True
    limit = USER_MEMBER_LIMITS_BY_ACCOUNT_TYPE.get(key)
    return limit is None or limit > 1


def max_devices_for_account_type(account_type: str) -> int | None:
    """Return max devices allowed per owner for an account type."""
    return DEVICE_LIMITS_BY_ACCOUNT_TYPE.get(str(account_type).strip().lower())


def max_user_members_for_account_type(
    account_type: str,
    *,
    tier_level: int | None = None,
) -> int | None:
    """Return max total active users for an admin of this tier.

    Includes the administrator seat. ``None`` means unlimited.
    """
    key = str(account_type).strip().lower()
    if key == "enhanced_plus":
        from app.services.registration_code_service import ENHANCED_PLUS_LEVELS

        try:
            level = int(tier_level) if tier_level is not None else 1
        except (TypeError, ValueError):
            level = 1
        if level not in ENHANCED_PLUS_LEVELS:
            level = 1
        return ENHANCED_PLUS_LEVELS[level]
    return USER_MEMBER_LIMITS_BY_ACCOUNT_TYPE.get(key)


def max_total_users_for_owner(owner: Owner) -> int | None:
    """Resolve total-user cap for this account holder (None = unlimited)."""
    return max_user_members_for_account_type(
        owner.account_type.value,
        tier_level=getattr(owner, "tier_level", None),
    )


ACCOUNT_IN_USE_DETAIL = (
    "This account is already in use on another device. Sign out there first, "
    "or use this device instead from the login screen."
)


def _presence_cutoff() -> datetime:
    return datetime.utcnow() - timedelta(
        seconds=max(60, int(settings.DEVICE_PRESENCE_TIMEOUT_SECONDS)),
    )


def device_presence_is_active(device) -> bool:
    """True when a device is marked online and its presence is still fresh."""
    if not device.is_online:
        return False
    return _device_recency(device) >= _presence_cutoff()


def expire_stale_device_sessions(db: Session, owner_id: int) -> None:
    """Mark online devices as offline when their last_seen is too old."""
    devices = device_crud.list_devices(db, owner_id=owner_id)
    cutoff = _presence_cutoff()
    for device in devices:
        if not device.is_online:
            continue
        if _device_recency(device) < cutoff:
            device.is_online = False
            db.flush()


def release_other_device_sessions(
    db: Session,
    owner_id: int,
    keep_hid: str | None = None,
) -> list[str]:
    """Delete every other phone/web login session so the caller can take over.

    Smart-home hubs are left alone — they are not login sessions.
    Returns the HID list of removed client sessions (for SESSION_REVOKED).
    """
    expire_stale_device_sessions(db, owner_id)
    normalized_keep = str(keep_hid).strip().upper() if keep_hid else None
    devices = device_crud.list_devices(db, owner_id=owner_id)
    released: list[str] = []
    for device in devices:
        if not is_client_session_hid(device.hid):
            continue
        if normalized_keep and str(device.hid).strip().upper() == normalized_keep:
            continue
        hid = str(device.hid).strip() if device.hid else ""
        device_crud.delete_device(db, device.id, owner_id=owner_id)
        if hid:
            released.append(hid)
    return released


def assert_no_conflicting_online_session(
    db: Session,
    owner_id: int,
    enrolling_hid: str,
) -> None:
    """Reject client-session enrollment when another phone/web session is online.

    Smart-home hubs (non MOB-/WEB- HIDs) do not participate in the single
    login-session lock — they can be registered while a phone is signed in.
    """
    if not is_client_session_hid(enrolling_hid):
        return
    expire_stale_device_sessions(db, owner_id)
    devices = device_crud.list_devices(db, owner_id=owner_id)
    normalized_hid = str(enrolling_hid).strip().upper()
    for device in devices:
        if str(device.hid).strip().upper() == normalized_hid:
            continue
        if not is_client_session_hid(device.hid):
            continue
        if device_presence_is_active(device):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ACCOUNT_IN_USE_DETAIL,
            )


def _device_recency(device) -> datetime:
    for value in (device.last_seen, device.updated_at, device.created_at):
        if value is not None:
            return value
    return datetime.min


def count_smart_home_devices(db: Session, owner_id: int) -> int:
    """Count registered smart-home hubs (excludes MOB-/WEB- login clients)."""
    devices = device_crud.list_devices(db, owner_id=owner_id)
    return sum(1 for device in devices if is_smart_home_hid(device.hid))


def evict_offline_devices_to_make_room(db: Session, owner: Owner) -> None:
    """Remove oldest offline smart-home hubs until under the tier cap.

    Phone/web login clients (MOB-/WEB-) never count toward the smart-home
    capacity and are never evicted by this helper.
    """
    expire_stale_device_sessions(db, owner.id)
    max_devices = max_devices_for_account_type(owner.account_type.value)
    if max_devices is None:
        return
    smart_homes = [
        device
        for device in device_crud.list_devices(db, owner_id=owner.id)
        if is_smart_home_hid(device.hid)
    ]
    while len(smart_homes) >= max_devices:
        offline = [device for device in smart_homes if not device.is_online]
        if not offline:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Account type '{owner.account_type.value}' allows at most "
                    f"{max_devices} smart-home device(s) per owner"
                ),
            )
        oldest = min(offline, key=_device_recency)
        device_crud.delete_device(db, oldest.id, owner_id=owner.id)
        smart_homes = [device for device in smart_homes if device.id != oldest.id]


def assert_owner_device_capacity(owner: Owner, current_device_count: int) -> None:
    """Ensure owner has capacity to enroll another smart-home device."""
    max_devices = max_devices_for_account_type(owner.account_type.value)
    if max_devices is None:
        return
    if current_device_count >= max_devices:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Account type '{owner.account_type.value}' allows at most "
                f"{max_devices} smart-home device(s) per owner"
            ),
        )


def assert_account_allows_user_members(account_type: str) -> None:
    """Ensure account tier supports user-member registrations at all."""
    if not account_type_supports_member_invite(account_type):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Account type '{account_type}' does not allow user members",
        )


def _count_active_user_members(db: Session, admin_owner_id: int) -> int:
    """Count active user-role members linked to the given administrator."""
    from app.models.owner import OwnerRole  # local import avoids cycle at module load

    return (
        db.query(Owner.id)
        .filter(
            Owner.account_owner_id == admin_owner_id,
            Owner.id != admin_owner_id,
            Owner.role == OwnerRole.USER,
            Owner.active.is_(True),
        )
        .count()
    )


def _count_active_account_users(db: Session, admin_owner: Owner) -> int:
    """Count active seats on this network (admin + invited members)."""
    admin_active = 1 if bool(admin_owner.active) else 0
    return admin_active + _count_active_user_members(db, admin_owner.id)


def assert_admin_user_member_capacity(db: Session, admin_owner: Owner) -> None:
    """Ensure the administrator has capacity to add another user member."""
    account_type = admin_owner.account_type.value
    limit = max_total_users_for_owner(admin_owner)
    if limit is None:
        return
    if not account_type_supports_member_invite(account_type):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Account type '{account_type}' does not allow user members",
        )
    current = _count_active_account_users(db, admin_owner)
    if current >= limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Account type '{account_type}' allows at most {limit} user(s) "
                f"on this account"
            ),
        )
