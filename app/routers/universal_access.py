"""Universal access code requests and credentials."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.crud import owner as owner_crud
from app.database import get_db
from app.services import universal_access_service

router = APIRouter(prefix="/universal-access", tags=["universal-access"])


class UniversalAccessCreate(BaseModel):
    member_id: int = Field(..., ge=1)
    address: str = Field(..., min_length=1, max_length=255)
    photo_id: str = Field(..., min_length=1)
    country: str = Field(..., min_length=1, max_length=120)
    region: str = Field(..., min_length=1, max_length=120)
    city: str = Field(..., min_length=1, max_length=120)
    note: str = Field("", max_length=1000)


class UniversalAccessReject(BaseModel):
    reason: str = Field("", max_length=500)


class UniversalAccessEnabled(BaseModel):
    enabled: bool


def _caller(db: Session, current_user: dict):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    return owner


@router.post("/requests")
async def create_universal_access_request(
    body: UniversalAccessCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    created = universal_access_service.create_request(
        db,
        admin=caller,
        member_id=body.member_id,
        address=body.address,
        photo_id=body.photo_id,
        country=body.country,
        region=body.region,
        city=body.city,
        note=body.note,
    )
    notice_event_id = created.pop("notice_event_id", None)
    if notice_event_id:
        await universal_access_service.notify_system_admins_of_request(db, str(notice_event_id))
    return created


@router.get("/requests")
def list_universal_access_requests(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.list_requests(db, caller=caller)


@router.get("/requests/{request_id}")
def get_universal_access_request(
    request_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.get_request(db, caller=caller, request_id=request_id)


@router.post("/requests/{request_id}/approve")
def approve_universal_access_request(
    request_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.approve_request(db, caller=caller, request_id=request_id)


@router.post("/requests/{request_id}/reject")
def reject_universal_access_request(
    request_id: int,
    body: UniversalAccessReject,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.reject_request(
        db,
        caller=caller,
        request_id=request_id,
        reason=body.reason,
    )


@router.post("/credentials/{credential_id}/enabled")
def set_universal_access_enabled(
    credential_id: int,
    body: UniversalAccessEnabled,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.set_credential_enabled(
        db,
        caller=caller,
        credential_id=credential_id,
        enabled=body.enabled,
    )
