"""Network admins send a short fixed command list to their own smart hub."""
from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.hub_command import HubCommand
from app.models.owner import Owner
from app.services.account_type_policy import is_system_administrator
from app.services.device_entitlements import is_smart_home_hid
from app.services.smart_home_webhook_service import is_valid_webhook_url, normalize_webhook_url

HUB_COMMANDS = ("arm", "disarm", "status")
_HISTORY_LIMIT = 8


def _require_network_admin(owner: Owner) -> None:
    if is_system_administrator(owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System administrators do not send hub commands.",
        )
    if (owner.role.value or "").strip().lower() != "administrator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a network administrator can send hub commands.",
        )


def _row(command: HubCommand) -> dict[str, Any]:
    return {
        "id": command.id,
        "hid": command.hid,
        "command": command.command,
        "status": command.status,
        "detail": command.detail or "",
        "created_at": command.created_at.isoformat() if command.created_at else None,
    }


def list_hub_commands(db: Session, owner: Owner) -> list[dict[str, Any]]:
    _require_network_admin(owner)
    rows = (
        db.query(HubCommand)
        .filter(HubCommand.owner_id == owner.id)
        .order_by(HubCommand.id.desc())
        .limit(_HISTORY_LIMIT)
        .all()
    )
    return [_row(row) for row in rows]


def send_hub_command(db: Session, owner: Owner, *, hid: str, command: str) -> dict[str, Any]:
    _require_network_admin(owner)
    verb = str(command or "").strip().lower()
    if verb not in HUB_COMMANDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Command must be arm, disarm, or status.",
        )
    normalized_hid = str(hid or "").strip()
    if not is_smart_home_hid(normalized_hid):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose a smart-home hub.",
        )
    device = (
        db.query(Device)
        .filter(Device.owner_id == owner.id, Device.hid == normalized_hid)
        .first()
    )
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That hub is not on your account.",
        )

    webhook = normalize_webhook_url(str(getattr(device, "webhook", "") or ""))
    outcome = "failed"
    detail = "No webhook configured."
    if is_valid_webhook_url(webhook):
        body = {
            "title": "Hex Zone command",
            "message": verb.upper(),
            "command": verb,
        }
        try:
            with httpx.Client(timeout=8.0) as client:
                response = client.post(webhook, json=body)
            reply = (response.text or "").strip().replace("\n", " ")[:240]
            if response.status_code >= 400:
                outcome = "failed"
                detail = f"Hub replied HTTP {response.status_code}. {reply}".strip()
            else:
                outcome = "sent"
                detail = reply or "Hub accepted the command."
        except httpx.TimeoutException:
            outcome = "no_reply"
            detail = "The hub did not reply."
        except httpx.HTTPError as exc:
            outcome = "failed"
            detail = f"Could not reach the hub. {exc}"[:240]

    row = HubCommand(
        owner_id=owner.id,
        hid=normalized_hid,
        command=verb,
        status=outcome,
        detail=detail,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row(row)
