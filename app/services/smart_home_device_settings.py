"""Per-device smart-home webhook, zone links, and poll interval."""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Device, Zone
from app.models.owner import Owner
from app.services.access_policy import visible_zone_owner_ids
from app.services.device_entitlements import is_smart_home_hid
from app.services.smart_home_webhook_service import is_valid_webhook_url, normalize_webhook_url


def validate_smart_home_binding(
    db: Session,
    owner: Owner,
    *,
    webhook: str,
    zone_ids: list[int] | None,
    periodical_check_sec: str | None,
    exclude_device_id: int | None = None,
) -> tuple[str, list[int], str]:
    """Normalize and check a hub's webhook, zones, and poll interval.

    Returns ``(webhook, zone_ids, periodical_check_sec)``.
    """
    url = normalize_webhook_url(webhook or "")
    if not url or not is_valid_webhook_url(url):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Webhook must be a full http:// or https:// URL "
                "(example: https://hub.example.com/hooks/hex-zone)."
            ),
        )

    duplicate = (
        db.query(Device.id)
        .filter(Device.webhook == url)
        .all()
    )
    for (device_id,) in duplicate:
        if exclude_device_id is not None and int(device_id) == int(exclude_device_id):
            continue
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Another device already uses this webhook. Each hub needs its own URL.",
        )

    requested: list[int] = []
    seen: set[int] = set()
    for item in zone_ids or []:
        try:
            zone_id = int(item)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Zone selection is invalid.",
            ) from None
        if zone_id in seen:
            continue
        seen.add(zone_id)
        requested.append(zone_id)
    if not requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select at least one current zone for this smart-home device.",
        )

    network_id = str(getattr(owner, "zone_id", "") or "").strip()
    visible_owners = {int(owner_id) for owner_id in visible_zone_owner_ids(db, owner)}
    visible_owners.add(int(owner.id))
    allowed_rows = (
        db.query(Zone.id)
        .filter(
            Zone.id.in_(requested),
            Zone.active.is_(True),
            or_(
                Zone.owner_id.in_(visible_owners),
                Zone.creator_id == owner.id,
                Zone.zone_id == network_id,
            ),
        )
        .all()
    )
    allowed = {int(row[0]) for row in allowed_rows}
    if any(zone_id not in allowed for zone_id in requested):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more selected zones are not on this account.",
        )

    period = (periodical_check_sec or "86400").strip() or "86400"
    if not period.isdigit() or not (1 <= int(period) <= 86400 * 30):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Periodical check must be a number of seconds.",
        )
    return url, requested, period


def apply_smart_home_create_fields(db: Session, owner: Owner, device) -> None:
    """Require webhook and zones on smart-home creates. Login sessions stay blank."""
    if not is_smart_home_hid(device.hid):
        device.webhook = ""
        device.zone_ids = []
        return
    webhook, zone_ids, period = validate_smart_home_binding(
        db,
        owner,
        webhook=device.webhook or "",
        zone_ids=list(device.zone_ids or []),
        periodical_check_sec=device.periodical_check_sec,
    )
    device.webhook = webhook
    device.zone_ids = zone_ids
    device.periodical_check_sec = period


def apply_smart_home_update_fields(
    db: Session,
    owner: Owner,
    existing: Device,
    device_update,
) -> None:
    """Validate webhook/zone edits. Phone and browser sessions cannot store them."""
    fields = set(getattr(device_update, "model_fields_set", set()) or set())
    touched = fields.intersection({"webhook", "zone_ids", "periodical_check_sec"})
    if not is_smart_home_hid(existing.hid):
        if "webhook" in fields:
            device_update.webhook = ""
        if "zone_ids" in fields:
            device_update.zone_ids = []
        return
    if not touched:
        return
    webhook = (
        device_update.webhook
        if "webhook" in fields
        else (existing.webhook or "")
    )
    zone_ids = (
        device_update.zone_ids
        if "zone_ids" in fields
        else list(existing.zone_ids or [])
    )
    period = (
        device_update.periodical_check_sec
        if "periodical_check_sec" in fields
        else (existing.periodical_check_sec or "86400")
    )
    webhook, zone_ids, period = validate_smart_home_binding(
        db,
        owner,
        webhook=webhook or "",
        zone_ids=list(zone_ids or []),
        periodical_check_sec=period,
        exclude_device_id=existing.id,
    )
    device_update.webhook = webhook
    device_update.zone_ids = zone_ids
    device_update.periodical_check_sec = period
