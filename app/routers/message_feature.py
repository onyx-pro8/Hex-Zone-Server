"""Advanced geo propagation and permission APIs."""
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.crud import owner as owner_crud
from app.database import get_db
from app.models import AccessSchedule, GuestAccessSession, MessageBlock, ZoneMessageEvent
from app.models.owner import OwnerRole
from app.schemas.access_guest import (
    GuestAccessHttpError,
    GuestAccessSessionListItem,
    GuestAdminDecisionResponse,
    GuestRequestDecisionData,
    GuestRequestDecisionEnvelope,
)
from pydantic import ValidationError

from app.schemas.message_feature import (
    AccessScheduleCreate,
    AccessScheduleResponse,
    BlockRuleCreate,
    BlockRuleResponse,
    PermissionDecisionResponse,
    PropagationMessageCreate,
    PropagationMessageResponse,
)
from app.services import guest_access_service, message_feature_service, permission_service
from app.services.zone_membership_service import refresh_owner_memberships
from app.websocket.manager import ws_manager

router = APIRouter(prefix="/message-feature", tags=["message-feature"])


@router.post("/members/location", summary="Update member location and zone memberships")
async def update_member_location(
    payload: dict,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sender = owner_crud.get_owner(db, current_user["user_id"])
    if not sender:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender owner not found")

    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="latitude/longitude are required")

    matched = refresh_owner_memberships(db, sender, float(latitude), float(longitude))
    db.commit()
    return {"zone_ids": matched}


@router.post("/messages/propagate", response_model=PropagationMessageResponse, status_code=status.HTTP_201_CREATED)
async def create_geo_message(
    payload: dict,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    msg_type = str(payload.get("type") or "").strip().upper()
    if msg_type == "PERMISSION":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "PERMISSION_MANUAL_DISABLED",
                "message": "PERMISSION messages are server-generated only for guest workflow transitions.",
            },
        )

    try:
        parsed_payload = PropagationMessageCreate.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "VALIDATION_ERROR",
                "message": "Invalid propagation payload.",
                "details": exc.errors(),
            },
        ) from exc

    sender = owner_crud.get_owner(db, current_user["user_id"])
    if not sender:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender owner not found")

    try:
        result = message_feature_service.create_geo_propagated_message(db, sender, parsed_payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "MISSING_RECIPIENT_FOR_PRIVATE_TYPE",
                "message": "receiver_owner_id is required for private-scope message types.",
            },
        ) from exc
    db.commit()

    await ws_manager.broadcast_to_users(result["delivered_owner_ids"], "NEW_GEO_MESSAGE", result)
    return result


@router.post(
    "/messages/ingest",
    response_model=PropagationMessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Device ingest endpoint using API key",
)
async def create_geo_message_with_api_key(
    payload: dict,
    x_api_key: str = Header(..., alias="x-api-key"),
    db: Session = Depends(get_db),
):
    msg_type = str(payload.get("type") or "").strip().upper()
    if msg_type == "PERMISSION":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "PERMISSION_MANUAL_DISABLED",
                "message": "PERMISSION messages are server-generated only for guest workflow transitions.",
            },
        )

    try:
        parsed_payload = PropagationMessageCreate.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "VALIDATION_ERROR",
                "message": "Invalid propagation payload.",
                "details": exc.errors(),
            },
        ) from exc

    sender = owner_crud.get_owner_by_api_key(db, x_api_key)
    if not sender:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    try:
        result = message_feature_service.create_geo_propagated_message(db, sender, parsed_payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "MISSING_RECIPIENT_FOR_PRIVATE_TYPE",
                "message": "receiver_owner_id is required for private-scope message types.",
            },
        ) from exc
    db.commit()
    await ws_manager.broadcast_to_users(result["delivered_owner_ids"], "NEW_GEO_MESSAGE", result)
    return result


@router.post("/blocks", response_model=BlockRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_block_rule(
    payload: BlockRuleCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    block = MessageBlock(
        owner_id=current_user["user_id"],
        blocked_owner_id=payload.blocked_owner_id,
        blocked_message_type=(payload.blocked_message_type.value if payload.blocked_message_type else None),
    )
    db.add(block)
    db.commit()
    db.refresh(block)
    return block


@router.get("/blocks", response_model=list[BlockRuleResponse])
async def list_block_rules(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(MessageBlock)
        .filter(MessageBlock.owner_id == current_user["user_id"])
        .order_by(MessageBlock.created_at.desc())
        .all()
    )
    return rows


@router.delete("/blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_block_rule(
    block_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.get(MessageBlock, block_id)
    if not row or row.owner_id != current_user["user_id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Block rule not found")
    db.delete(row)
    db.commit()


@router.post(
    "/access/schedules",
    response_model=AccessScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create access schedule",
    description=(
        "Authenticated member defines an expected visitor window for a **zone_id**. "
        "Used by matching logic in `POST /api/access/permission` (QR guests) and by "
        "`process_permission_message` for device-originated PERMISSION messages."
    ),
    response_description="Persisted schedule including audit fields.",
)
async def create_access_schedule(
    payload: AccessScheduleCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    schedule = permission_service.create_schedule(db, owner, payload.model_dump())
    db.commit()
    db.refresh(schedule)
    return schedule


@router.get(
    "/access/schedules",
    response_model=list[AccessScheduleResponse],
    summary="List access schedules",
    description="Returns active schedules, optionally filtered by **zone_id**.",
    response_description="Newest-first list of schedule rows.",
)
async def list_access_schedules(
    zone_id: str | None = Query(default=None, description="If set, restrict to this zone id."),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ = current_user
    query = db.query(AccessSchedule).filter(AccessSchedule.active.is_(True))
    if zone_id:
        query = query.filter(AccessSchedule.zone_id == zone_id)
    return query.order_by(AccessSchedule.created_at.desc()).all()


@router.get(
    "/access/guest-requests",
    response_model=list[GuestAccessSessionListItem],
    summary="List QR guest arrival sessions (legacy array)",
    description=(
        "Returns **`guest_access_sessions`** for **`?zone_id=`** (Bearer member JWT). "
        "**Authorization** matches **`GET /api/access/guest-requests`**: caller must be allowed to administer the zone "
        "(visible **`zones`** row for this **`zone_id`**, or primary **`owners.zone_id`**, or linked member zone for admins).\n\n"
        "Response is a **raw JSON array** (no **`{ status, data }`** envelope). Prefer **`GET /api/access/guest-requests`** "
        "for new SPA clients (envelope + identical row schema **`GuestAccessSessionListItem`**).\n\n"
        "**Query parity** with **`GET /api/access/guest-requests`**: **`status`** (filter), **`pending_only`**, "
        "**`limit`**, **`skip`**. Resolve sessions with **`POST /message-feature/access/guest-requests/{guest_id}/approve?zone_id=`** "
        "or **`/reject?zone_id=`** "
        "(path **guest_id** + required **`zone_id`** query), or **`POST /api/access/approve`** | **`reject`** "
        "with **`GuestZoneActionRequest`** (**guest_id**, **zone_id** in body)."
    ),
    response_description="Newest **`created_at`** first; each item matches **`GuestAccessSessionListItem`** in OpenAPI.",
)
async def list_guest_requests(
    zone_id: str = Query(
        ...,
        min_length=1,
        max_length=100,
        description="Hex zone id from QR / dashboard (**`zid`**). Required for listing; paired approve/reject use path **guest_id** only.",
    ),
    filter_status: str | None = Query(
        default=None,
        max_length=32,
        alias="status",
        description="Optional filter: **PENDING**, **APPROVED**, **REJECTED** (case-insensitive; GRANTED/DENIED accepted).",
    ),
    pending_only: bool = Query(
        False,
        description="If true, only unexpected sessions still in **pending** resolution.",
    ),
    limit: int = Query(50, ge=1, le=200, description="Max rows (most recent first)."),
    skip: int = Query(0, ge=0, le=10_000, description="Offset for pagination."),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    viewer = owner_crud.get_owner(db, current_user["user_id"])
    if not viewer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    zid = zone_id.strip()
    if viewer.role != OwnerRole.ADMINISTRATOR or viewer.zone_id != zid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "FORBIDDEN",
                "message": "Administrator role is required for this zone.",
            },
        )
    if not guest_access_service.can_manage_zone_guest_requests(db, viewer, zid):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "FORBIDDEN",
                "message": "You are not allowed to list guest requests for this zone.",
            },
        )
    rows = guest_access_service.list_guest_sessions_for_zone(
        db,
        zone_id=zid,
        limit=limit,
        skip=skip,
        pending_only=pending_only,
        status=filter_status,
    )
    return [
        GuestAccessSessionListItem.model_validate(guest_access_service.serialize_guest_session_row(db, r))
        for r in rows
    ]


def _effective_zone_for_guest_admin_action(
    db: Session,
    *,
    guest_id: str,
    zone_id_query: str | None,
) -> str:
    """Resolve **zone_id** from the persisted session; legacy **zone_id** query must match if present."""
    row = guest_access_service.get_guest_access_session_by_guest_id(db, guest_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Guest session not found."},
        )
    hinted = (zone_id_query or "").strip()
    if hinted and hinted != row.zone_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "FORBIDDEN",
                "message": "zone_id does not match this guest session.",
            },
        )
    return row.zone_id


_REQUIRED_ZONE_QUERY = Query(
    ...,
    min_length=1,
    max_length=100,
    description=(
        "Required zone id query parameter. It must exactly match "
        "the zone on the stored guest request."
    ),
)

_GUEST_ID_PATH = Path(
    ...,
    min_length=1,
    max_length=36,
    description=(
        "**guest_id**: opaque id issued at **`POST /api/access/permission`** "
        "(and listed on **`GET /message-feature/access/guest-requests`**). "
        "**zone_id** is not required here; it comes from this session row unless you pass **`?zone_id=`** "
        "(legacy, must match the row)."
    ),
)


@router.post(
    "/access/guest-requests/{requestId}/approve",
    response_model=GuestRequestDecisionEnvelope,
    status_code=status.HTTP_200_OK,
    summary="Approve unexpected guest (dashboard path)",
    description=(
        "**Bearer** JWT. Equivalent to **`POST /api/access/approve`**: resolves **zone_id** from "
        "**`guest_access_sessions`** keyed by **path `requestId`** (session table row id), then verifies the caller is an "
        "**administrator** for that zone and applies approval. "
        "**`?zone_id=`** is required and must match the persisted session zone; mismatched **`zone_id`** → **`403`**."
    ),
    response_description=(
        "`APPROVED` decision envelope with request id and zone metadata."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "Administrator required for session zone (`FORBIDDEN`), "
                "or optional **`zone_id`** query does not equal the persisted session (`FORBIDDEN`). "
                "Structured body: **`error_code`** + **`message`** (see **`GuestAccessHttpError`**)."
            ),
            "model": GuestAccessHttpError,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": (
                "Unknown **requestId** → **`ACCESS_REQUEST_NOT_FOUND`** with **`error_code`** / **`message`**. "
                "Missing JWT **owner** may return unstructured **`detail`** (string)."
            ),
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": (
                "Session not **`unexpected`**, not **`pending`**, or **`access_revoked_at`** already set (`INVALID_STATE`)."
            ),
            "model": GuestAccessHttpError,
        },
    },
)
async def approve_guest_request_message_feature(
    requestId: str = _GUEST_ID_PATH,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    zone_id: str = _REQUIRED_ZONE_QUERY,
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")

    session_row = db.get(GuestAccessSession, int(requestId)) if requestId.isdigit() else None
    if not session_row:
        session_row = guest_access_service.get_guest_access_session_by_guest_id(db, requestId)
    if not session_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "ACCESS_REQUEST_NOT_FOUND", "message": "Guest access request not found."},
        )
    effective_zone_id = _effective_zone_for_guest_admin_action(db, guest_id=session_row.guest_id, zone_id_query=zone_id)

    result = guest_access_service.approve_guest(
        db,
        acting_owner=owner,
        zone_id=effective_zone_id,
        guest_id=session_row.guest_id.strip(),
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )
    db.commit()
    return GuestRequestDecisionEnvelope(
        data=GuestRequestDecisionData(
            id=str(requestId),
            status="APPROVED",
            zone_id=effective_zone_id,
            updated_at=datetime.utcnow(),
        )
    )


@router.post(
    "/access/guest-requests/{requestId}/reject",
    response_model=GuestRequestDecisionEnvelope,
    status_code=status.HTTP_200_OK,
    summary="Reject or revoke guest access (dashboard path)",
    description=(
        "**Bearer** JWT. Same semantics as **`POST /api/access/reject`**: **zone** inferred from the "
        "**requestId** session row and **`?zone_id=`** is required (must match that row). "
        "Supports denying **pending** guests, revoking **approved** unexpected guests (invalidates guest JWT), "
        "and revoking **expected** sessions (**`access_revoked_at`**)."
    ),
    response_description=(
        "`REJECTED` decision envelope with request id and zone metadata."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "Administrator required for session zone (`FORBIDDEN`), "
                "or **`zone_id`** hint mismatch (`FORBIDDEN`). Structured body **`GuestAccessHttpError`**."
            ),
            "model": GuestAccessHttpError,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Unknown **requestId** (`ACCESS_REQUEST_NOT_FOUND`) or owner not found.",
            "model": GuestAccessHttpError,
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Unexpected session already **`rejected`** (`INVALID_STATE`), or other invalid state.",
            "model": GuestAccessHttpError,
        },
    },
)
async def reject_guest_request_message_feature(
    requestId: str = _GUEST_ID_PATH,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    zone_id: str = _REQUIRED_ZONE_QUERY,
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Owner not found"},
        )

    session_row = db.get(GuestAccessSession, int(requestId)) if requestId.isdigit() else None
    if not session_row:
        session_row = guest_access_service.get_guest_access_session_by_guest_id(db, requestId)
    if not session_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "ACCESS_REQUEST_NOT_FOUND", "message": "Guest access request not found."},
        )
    effective_zone_id = _effective_zone_for_guest_admin_action(db, guest_id=session_row.guest_id, zone_id_query=zone_id)

    result = guest_access_service.reject_guest(
        db,
        acting_owner=owner,
        zone_id=effective_zone_id,
        guest_id=session_row.guest_id.strip(),
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )
    db.commit()
    return GuestRequestDecisionEnvelope(
        data=GuestRequestDecisionData(
            id=str(requestId),
            status="REJECTED",
            zone_id=effective_zone_id,
            updated_at=datetime.utcnow(),
        )
    )


@router.get("/messages/new")
async def list_new_feature_messages(
    since: str = Query(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ = current_user
    try:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid since cursor") from exc
    rows = (
        db.query(ZoneMessageEvent)
        .filter(ZoneMessageEvent.created_at >= since_dt)
        .order_by(ZoneMessageEvent.created_at.asc())
        .limit(500)
        .all()
    )
    return [
        {
            "id": row.id,
            "zoneId": row.zone_id,
            "type": row.type,
            "category": row.category.value,
            "scope": row.scope.value,
            "text": row.text,
            "body": row.body_json,
            "metadata": row.metadata_json,
            "createdAt": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.post(
    "/access/permission",
    response_model=PermissionDecisionResponse,
    status_code=status.HTTP_200_OK,
    summary="PERMISSION manual send disabled",
    description=(
        "Manual authenticated PERMISSION propagation is disabled. "
        "PERMISSION events are server-generated only for guest workflow transitions "
        "(submit, approve, reject). Use **`POST /api/access/permission`** for guest submit and "
        "**approve/reject** guest-request endpoints for decisions."
    ),
    response_description="Always returns `PERMISSION_MANUAL_DISABLED`.",
)
async def process_permission(
    payload: PropagationMessageCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "error_code": "PERMISSION_MANUAL_DISABLED",
            "message": "PERMISSION messages are server-generated only for guest workflow transitions.",
        },
    )
