"""Universal access invite keys and place-access requests."""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.crud import owner as owner_crud
from app.database import get_db
from app.services import universal_access_service

router = APIRouter(prefix="/universal-access", tags=["universal-access"])


class InviteCreate(BaseModel):
    key_type: str = Field("private", max_length=16)
    invitee_email: str = Field("", max_length=255)
    zone_ids: list[int] = Field(..., min_length=1)
    note: str = Field("", max_length=1000)


class PlaceAccessCreate(BaseModel):
    network_id: str = Field(..., min_length=1, max_length=100)
    zone_ids: list[int] = Field(..., min_length=1)
    photo_id: str = Field(..., min_length=1)
    business_document: str = Field(..., min_length=1)
    note: str = Field("", max_length=1000)


class PlaceAccessDocsUpdate(BaseModel):
    photo_id: str = Field(..., min_length=1)
    business_document: str = Field(..., min_length=1)


class UniversalAccessApprove(BaseModel):
    zone_ids: list[int] = Field(default_factory=list)


class UniversalAccessReject(BaseModel):
    reason: str = Field("", max_length=500)


class UniversalAccessEnabled(BaseModel):
    enabled: bool


class ActivatePublicKey(BaseModel):
    code: str = Field(..., min_length=3, max_length=32)


def _caller(db: Session, current_user: dict):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    return owner


@router.get("/shareable-zones")
def list_shareable_zones(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.shareable_network_zones(db, admin=caller)


@router.get("/primary-zones")
def list_shareable_primary_zones(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.shareable_network_zones(db, admin=caller)


@router.get("/invitees")
def search_invitees(
    q: str = Query("", max_length=255),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.search_invitees(db, admin=caller, query=q, limit=5)


@router.get("/targets")
def search_targets(
    q: str = Query("", max_length=255),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.search_target_networks(db, caller=caller, query=q, limit=8)


@router.get("/targets/{network_id}")
def get_target_network(
    network_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.lookup_target_network(
        db, caller=caller, network_id=network_id
    )


@router.post("/requests/invite")
async def create_invite_request(
    body: InviteCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    created = universal_access_service.generate_invite_key(
        db,
        admin=caller,
        key_type=body.key_type,
        zone_ids=body.zone_ids,
        invitee_email=body.invitee_email,
        note=body.note,
    )
    notice_event_id = created.pop("notice_event_id", None)
    if notice_event_id:
        await universal_access_service.notify_permission_recipients(
            db,
            str(notice_event_id),
            title="Universal access shared",
        )
        await universal_access_service.notify_granted_zones(
            db,
            member_id=int(created["member_id"]),
            zone_ids=list(created.get("zone_ids") or []),
        )
    return created


@router.post("/requests/place-access")
async def create_place_access_request(
    body: PlaceAccessCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    created = universal_access_service.create_place_access_request(
        db,
        requester=caller,
        network_id=body.network_id,
        zone_ids=body.zone_ids,
        photo_id=body.photo_id,
        business_document=body.business_document,
        note=body.note,
    )
    notice_event_id = created.pop("notice_event_id", None)
    if notice_event_id:
        await universal_access_service.notify_permission_recipients(
            db,
            str(notice_event_id),
            title="Universal access request",
        )
    return created


@router.post("/requests")
async def create_universal_access_request(
    body: InviteCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Backward-compatible invite endpoint."""
    return await create_invite_request(body, current_user, db)


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


@router.patch("/requests/{request_id}/docs")
def update_universal_access_docs(
    request_id: int,
    body: PlaceAccessDocsUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.update_request_docs(
        db,
        caller=caller,
        request_id=request_id,
        photo_id=body.photo_id,
        business_document=body.business_document,
    )


@router.delete("/requests/{request_id}")
def delete_universal_access_request(
    request_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.delete_request(db, caller=caller, request_id=request_id)


@router.post("/requests/{request_id}/approve")
async def approve_universal_access_request(
    request_id: int,
    body: UniversalAccessApprove | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del body
    caller = _caller(db, current_user)
    approved = universal_access_service.approve_request(
        db,
        caller=caller,
        request_id=request_id,
    )
    notice_event_id = approved.pop("notice_event_id", None)
    if notice_event_id:
        await universal_access_service.notify_decision_recipients(
            db,
            str(notice_event_id),
            title="Universal access approved",
        )
    await universal_access_service.notify_granted_zones(
        db,
        member_id=int(approved["member_id"]),
        zone_ids=list(approved.get("zone_ids") or []),
    )
    return approved


@router.post("/requests/{request_id}/reject")
async def reject_universal_access_request(
    request_id: int,
    body: UniversalAccessReject,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    rejected = universal_access_service.reject_request(
        db,
        caller=caller,
        request_id=request_id,
        reason=body.reason,
    )
    notice_event_id = rejected.pop("notice_event_id", None)
    if notice_event_id:
        await universal_access_service.notify_decision_recipients(
            db,
            str(notice_event_id),
            title="Universal access rejected",
        )
    return rejected


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


@router.get("/keys/private")
def list_private_keys(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.list_private_keys(db, caller=caller)


@router.get("/keys/public")
def search_public_keys(
    q: str = Query("", max_length=32),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.search_public_keys(db, caller=caller, query=q, limit=10)


@router.get("/keys/activated")
def list_activated_keys(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    return universal_access_service.list_activated_keys(db, caller=caller)


@router.post("/keys/activate")
async def activate_public_key(
    body: ActivatePublicKey,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = _caller(db, current_user)
    activated = universal_access_service.activate_public_key(
        db, caller=caller, code=body.code
    )
    await universal_access_service.notify_granted_zones(
        db,
        member_id=int(caller.id),
        zone_ids=list(activated.get("zone_ids") or []),
    )
    return activated
