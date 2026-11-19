"""Grant and revoke Technical Support permission.

Only the system administrator may grant it. The recipient becomes a Private
account on the system administrator's network id and receives the same
operational access, with the role Technical Support.
"""
from __future__ import annotations

import json

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Owner
from app.models.owner import AccountType, OwnerRole
from app.services.access_policy import account_root_id
from app.services.account_type_policy import (
    is_system_administrator,
    is_technical_support,
)

_GRANT_FORBIDDEN = "Only the system administrator can give Technical Support permission."


def _require_system_administrator(caller: Owner) -> None:
    if not is_system_administrator(caller):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_GRANT_FORBIDDEN,
        )


def _snapshot(owner: Owner) -> str:
    return json.dumps(
        {
            "account_type": owner.account_type.value,
            "role": owner.role.value,
            "zone_id": owner.zone_id,
            "account_owner_id": owner.account_owner_id,
        }
    )


def _restore_snapshot(owner: Owner) -> None:
    raw = (getattr(owner, "platform_staff_prior", None) or "").strip()
    if not raw:
        return
    try:
        prior = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(prior, dict):
        return
    account_type = str(prior.get("account_type") or "").strip()
    role = str(prior.get("role") or "").strip()
    zone_id = str(prior.get("zone_id") or "").strip()
    try:
        if account_type:
            owner.account_type = AccountType(account_type)
        if role:
            owner.role = OwnerRole(role)
    except ValueError:
        return
    if zone_id:
        owner.zone_id = zone_id
    if "account_owner_id" in prior:
        account_owner_id = prior.get("account_owner_id")
        owner.account_owner_id = (
            int(account_owner_id) if account_owner_id is not None else None
        )


def grant_technical_support(db: Session, caller: Owner, target: Owner) -> Owner:
    """Make ``target`` Technical Support on the system administrator's network."""
    _require_system_administrator(caller)
    if int(target.id) == int(caller.id) or is_system_administrator(target):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The system administrator cannot be assigned Technical Support.",
        )
    network_id = (caller.zone_id or "").strip()
    if not network_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The system administrator has no network id to share.",
        )
    if not is_technical_support(target):
        target.platform_staff_prior = _snapshot(target)
    target.platform_staff_role = "technical_support"
    target.account_type = AccountType.PRIVATE
    target.role = OwnerRole.USER
    target.zone_id = network_id
    target.account_owner_id = account_root_id(caller)
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


def revoke_technical_support(db: Session, caller: Owner, target: Owner) -> Owner:
    """Remove Technical Support and restore the account snapshot from the grant."""
    _require_system_administrator(caller)
    if not is_technical_support(target):
        return target
    _restore_snapshot(target)
    target.platform_staff_role = "none"
    target.platform_staff_prior = None
    db.add(target)
    db.commit()
    db.refresh(target)
    return target
