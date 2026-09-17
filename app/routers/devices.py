"""Router for Device endpoints."""
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.schemas import (
    DeviceCreate,
    DeviceClaimSessionRequest,
    DeviceClaimSessionResponse,
    DeviceResponse,
    DeviceUpdate,
    DeviceLocationUpdate,
)
from app.crud import device as device_crud
from app.crud import owner as owner_crud
from app.core.security import get_current_user
from app.services.access_policy import visible_owner_ids
from app.services.device_entitlements import (
    assert_no_conflicting_online_session,
    assert_owner_device_capacity,
    count_smart_home_devices,
    device_presence_is_active,
    evict_offline_devices_to_make_room,
    expire_stale_device_sessions,
    is_client_session_hid,
    release_other_device_sessions,
)
from app.websocket.manager import ws_manager

router = APIRouter(prefix="/devices", tags=["devices"])


def _caller_visibility(db: Session, user_id: int) -> list[int]:
    owner = owner_crud.get_owner(db, user_id)
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )
    return visible_owner_ids(db, owner)


@router.post(
    "/",
    response_model=DeviceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create device",
    description=(
        "Create a device under the authenticated owner account. Smart-home "
        "enrollment capacity (non MOB-/WEB- HIDs) is enforced by account tier: "
        "private/enhanced=1, exclusive=0 (disabled), private_plus=10, enhanced_plus=unlimited. "
        "Phone and browser login clients (MOB-/WEB-) do not consume that quota."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Authenticated owner was not found.",
        },
        status.HTTP_409_CONFLICT: {
            "description": "A device with the same hardware id already exists.",
        },
    },
    response_description="Created device record for the authenticated owner.",
)
async def create_device(
    device: DeviceCreate,
    response: Response,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new device for the current owner."""
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )
    try:
        assert_no_conflicting_online_session(db, owner.id, device.hid)
        if not is_client_session_hid(device.hid):
            # Capacity applies to smart-home hubs only.
            evict_offline_devices_to_make_room(db, owner)
            assert_owner_device_capacity(owner, count_smart_home_devices(db, owner.id))
        db_device = device_crud.create_device(db, current_user["user_id"], device)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        error_text = str(getattr(exc, "orig", exc)).lower()
        if (
            "ix_devices_hid" in error_text
            or "devices_hid_key" in error_text
            or ("unique" in error_text and "hid" in error_text)
        ):
            existing = device_crud.get_device_by_hid(db, device.hid, owner_id=owner.id)
            if not existing and is_client_session_hid(device.hid):
                # Same phone switching accounts: HID is globally unique and may
                # still belong to a previous owner. Reassign idle login sessions.
                foreign = device_crud.get_device_by_hid(db, device.hid)
                if foreign is not None and not device_presence_is_active(foreign):
                    foreign.owner_id = owner.id
                    db.flush()
                    existing = foreign
            if not existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Device hid '{device.hid}' already exists",
                ) from exc

            update_data = DeviceUpdate(
                **device.model_dump(exclude_unset=True, exclude={"hid", "status"})
            )
            updated = device_crud.update_device(
                db,
                existing.id,
                update_data,
                owner_id=owner.id,
            )
            if not updated:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Device hid '{device.hid}' already exists",
                ) from exc
            db.commit()
            db.refresh(updated)
            hydrated = device_crud.get_device(
                db, updated.id, owner_ids=[owner.id], load_owner=True
            )
            response.status_code = status.HTTP_200_OK
            return DeviceResponse.model_validate(hydrated or updated)
        raise
    db.refresh(db_device)
    hydrated = device_crud.get_device(db, db_device.id, owner_ids=[owner.id], load_owner=True)
    return DeviceResponse.model_validate(hydrated or db_device)


@router.get(
    "/",
    response_model=list[DeviceResponse],
    summary="List devices",
    description=(
        "List devices visible to caller by account policy. Administrators can view "
        "all devices under their account; users can view only their own devices."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Authenticated owner was not found.",
        },
    },
    response_description="Caller-visible device list.",
)
async def list_devices(
    skip: int = Query(0, ge=0),
    limit: int | None = Query(None, ge=1, le=10000),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List caller-visible devices based on account role."""
    # Include inactive linked users when listing devices for account administrators.
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )
    owner_ids = visible_owner_ids(db, owner, include_inactive=True)
    for owner_id in owner_ids:
        expire_stale_device_sessions(db, owner_id)
    db.commit()
    devices = device_crud.list_devices(
        db,
        owner_ids=owner_ids,
        skip=skip,
        limit=limit,
        load_owner=True,
    )
    return [DeviceResponse.model_validate(device) for device in devices]


@router.post(
    "/claim-session",
    response_model=DeviceClaimSessionResponse,
    summary="Claim device session",
    description=(
        "Remove other phone/web login devices for the authenticated owner so "
        "this device can take over the account session. Smart-home hubs are "
        "not deleted. Use when the user confirms device takeover on login."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Authenticated owner was not found.",
        },
    },
    response_description="Count of other login devices that were removed.",
)
async def claim_device_session(
    payload: DeviceClaimSessionRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete other phone/web login sessions for the current owner."""
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )
    released_hids = release_other_device_sessions(
        db, owner.id, keep_hid=payload.hid
    )
    # Only reclaim smart-home capacity slots; login clients were already removed.
    evict_offline_devices_to_make_room(db, owner)
    db.commit()
    kept_hid = str(payload.hid).strip() if payload.hid else None
    if released_hids:
        await ws_manager.broadcast_to_users(
            [owner.id],
            "SESSION_REVOKED",
            {
                "kept_hid": kept_hid,
                "released_hids": released_hids,
                "reason": "device_claim_session",
            },
        )
    return DeviceClaimSessionResponse(released=len(released_hids))


@router.post(
    "/{device_id}/heartbeat",
    response_model=DeviceResponse,
    summary="Record device heartbeat",
    description="Update the device online/last_seen presence marker for a caller-visible device.",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Owner or device was not found.",
        },
    },
    response_description="Device with refreshed presence metadata.",
)
async def device_heartbeat(
    device_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record device presence (online, last_seen)."""
    owner_ids = _caller_visibility(db, current_user["user_id"])
    device = device_crud.get_device(db, device_id, owner_ids=owner_ids, load_owner=True)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )
    device_crud.touch_presence(db, device)
    db.commit()
    db.refresh(device)
    return DeviceResponse.model_validate(device)


@router.get(
    "/{device_id}",
    response_model=DeviceResponse,
    summary="Get device",
    description="Get a caller-visible device by id.",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Owner or device was not found.",
        },
    },
    response_description="Requested device details.",
)
async def get_device(
    device_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a device by ID."""
    owner_ids = _caller_visibility(db, current_user["user_id"])
    device = device_crud.get_device(db, device_id, owner_ids=owner_ids)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )
    return DeviceResponse.model_validate(device)


@router.get(
    "/network/hid/{hid}",
    response_model=DeviceResponse,
    summary="Get device by hardware ID",
    description="Fetch a caller-visible device using hardware identifier (hid).",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Owner or device was not found.",
        },
    },
    response_description="Requested device details for the given hardware ID.",
)
async def get_device_by_hid(
    hid: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a device by hardware ID."""
    owner_ids = _caller_visibility(db, current_user["user_id"])
    device = device_crud.get_device_by_hid(db, hid, owner_ids=owner_ids, load_owner=True)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )
    return DeviceResponse.model_validate(device)


@router.patch(
    "/{device_id}",
    response_model=DeviceResponse,
    summary="Update device",
    description=(
        "Update a caller-visible device including address and operational settings. "
        "Administrators can manage linked user devices (including active/inactive state)."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Owner or device was not found.",
        },
    },
    response_description="Updated device record.",
)
async def update_device(
    device_id: int,
    device_update: DeviceUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a device."""
    owner_ids = _caller_visibility(db, current_user["user_id"])
    device = device_crud.update_device(
        db,
        device_id,
        device_update,
        owner_ids=owner_ids,
    )
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )
    db.commit()
    db.refresh(device)
    hydrated = device_crud.get_device(db, device.id, owner_ids=owner_ids, load_owner=True)
    return DeviceResponse.model_validate(hydrated or device)


@router.post(
    "/{device_id}/location",
    response_model=DeviceResponse,
    summary="Update device location",
    description="Update latitude/longitude/address and recompute H3 cell for a device.",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Owner or device was not found.",
        },
    },
    response_description="Device record after location update.",
)
async def update_device_location(
    device_id: int,
    location: DeviceLocationUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update device location and calculate H3 cell."""
    owner_ids = _caller_visibility(db, current_user["user_id"])
    device = device_crud.get_device(db, device_id, owner_ids=owner_ids)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )

    update_data = DeviceUpdate(
        latitude=location.latitude,
        longitude=location.longitude,
        address=location.address,
    )
    updated_device = device_crud.update_device(
        db,
        device_id,
        update_data,
        owner_ids=owner_ids,
    )
    device_crud.touch_presence(db, updated_device)

    db.commit()
    db.refresh(updated_device)
    hydrated = device_crud.get_device(db, updated_device.id, owner_ids=owner_ids, load_owner=True)
    return DeviceResponse.model_validate(hydrated or updated_device)


@router.delete(
    "/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete device",
    description="Delete a caller-visible device.",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Owner or device was not found.",
        },
    },
    response_description="Device deleted successfully.",
)
async def delete_device(
    device_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a device."""
    owner_ids = _caller_visibility(db, current_user["user_id"])
    deleted = device_crud.delete_device(db, device_id, owner_ids=owner_ids)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )
    db.commit()
