"""Advanced geo propagation and permission APIs."""
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.crud import owner as owner_crud
from app.database import get_db
from app.models import (
    AccessSchedule,
    EmergencyEvent,
    GuestAccessSession,
    MessageBlock,
    Owner,
    ZoneMessageEvent,
)
from app.models.owner import OwnerRole
from app.services.geospatial_service import (
    evaluate_zone_records_containing_point,
    owner_ids_located_within_zone_records,
    zone_ids_for_zone_records,
)
from sqlalchemy import and_, or_
from app.schemas.access_guest import (
    GuestAccessHttpError,
    GuestAccessSessionListItem,
    GuestAdminDecisionResponse,
    GuestRequestDecisionData,
    GuestRequestDecisionEnvelope,
)
from pydantic import BaseModel, Field, ValidationError

from app.schemas.message_feature import (
    AccessScheduleCreate,
    AccessScheduleResponse,
    BlockRuleCreate,
    BlockRuleResponse,
    PermissionDecisionResponse,
    PropagationMessageCreate,
    PropagationMessageResponse,
)
from app.domain.message_types import (
    MessageScope,
    is_pushable_geo_type,
    normalize_message_type,
)
from app.services import guest_access_service, message_block_service, message_feature_service, permission_service
from app.services.message_feature_service import (
    GeoMessageSkipped,
    PrivateScopeRecipientError,
    SensorRateLimitError,
    UnknownRateLimitError,
)
from app.domain.service_pa_topics import ServicePaValidationError
from app.services import push_notification_service
from app.services import wellness_ack_service
from app.services import alarm_read_service
from app.services.member_service import upsert_member_location
from app.websocket.manager import ws_manager

router = APIRouter(prefix="/message-feature", tags=["message-feature"])


async def _finalize_geo_propagation(db: Session, result: dict) -> dict:
    """WebSocket + optional mobile push after DB commit."""
    if result.get("skipped"):
        return result

    delivered = list(result.get("delivered_owner_ids") or [])
    sender_id = result.get("sender_id")
    ws_recipients = list({int(oid) for oid in delivered if isinstance(oid, int)})
    if isinstance(sender_id, int) and sender_id not in ws_recipients:
        ws_recipients.append(sender_id)
    if ws_recipients:
        await ws_manager.broadcast_to_users(ws_recipients, "NEW_GEO_MESSAGE", result)

    if is_pushable_geo_type(str(result.get("type") or "")):
        push_stats = await push_notification_service.send_alarm_push_to_owners(db, delivered, result)
        result.update(push_stats)
        push_notification_service.schedule_panic_retries_if_needed(delivered, result, push_stats)
    return result


def _handle_geo_propagation_errors(exc: Exception) -> None:
    if isinstance(exc, UnknownRateLimitError):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error_code": "RATE_LIMIT_UNKNOWN",
                "message": "Only one UNKNOWN message is allowed every 10 seconds.",
            },
        ) from exc
    if isinstance(exc, SensorRateLimitError):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error_code": "RATE_LIMIT_SENSOR",
                "message": "SENSOR telemetry is throttled. Please wait a few seconds before sending again.",
            },
        ) from exc
    if isinstance(exc, PrivateScopeRecipientError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "INVALID_PRIVATE_RECIPIENT",
                "message": str(exc) or "PRIVATE receiver must be in your zone or account.",
            },
        ) from exc
    if isinstance(exc, ServicePaValidationError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "SERVICE_PA_VALIDATION",
                "message": str(exc) or "Invalid PA or SERVICE message fields.",
            },
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "MISSING_RECIPIENT_FOR_PRIVATE_TYPE",
                "message": "receiver_owner_id is required for private-scope message types.",
            },
        ) from exc
    raise exc


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

    matched = upsert_member_location(db, sender.id, float(latitude), float(longitude))
    db.commit()
    return {"zone_ids": matched.get("zones") or []}


@router.get(
    "/members/in-zone",
    summary="List members currently located in the caller's zone(s)",
    description=(
        "Resolves the zone(s) that contain the caller's location, then returns "
        "every active owner whose **own current location** falls inside one of "
        "those zone(s) — across all accounts. Used to populate the PRIVATE "
        "message recipient picker so it matches server-side delivery rules. "
        "Pass **`latitude`/`longitude`** to use a live fix; otherwise the "
        "caller's stored `owners.latitude/longitude` is used."
    ),
)
async def list_in_zone_members(
    latitude: float | None = Query(default=None, ge=-90, le=90),
    longitude: float | None = Query(default=None, ge=-180, le=180),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    me = owner_crud.get_owner(db, current_user["user_id"])
    if not me:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")

    lat = latitude if latitude is not None else me.latitude
    lon = longitude if longitude is not None else me.longitude
    if lat is None or lon is None:
        return {"zone_ids": [], "members": []}

    zone_record_ids = evaluate_zone_records_containing_point(db, float(lat), float(lon))
    if not zone_record_ids:
        return {"zone_ids": [], "members": []}

    zone_ids = zone_ids_for_zone_records(db, zone_record_ids)
    located_ids = owner_ids_located_within_zone_records(
        db, zone_record_ids, exclude_owner_id=me.id
    )
    if not located_ids:
        return {"zone_ids": zone_ids, "members": []}

    rows = (
        db.query(Owner)
        .filter(Owner.id.in_(located_ids), Owner.active.is_(True))
        .all()
    )
    members = [
        {
            "id": row.id,
            "name": f"{row.first_name or ''} {row.last_name or ''}".strip() or row.email,
            "first_name": row.first_name,
            "last_name": row.last_name,
            "email": row.email,
            "zone_id": row.zone_id,
        }
        for row in rows
    ]
    members.sort(key=lambda m: (m["name"] or "").lower())
    return {"zone_ids": zone_ids, "members": members}


@router.get(
    "/members/search",
    summary="Search owners with the same zone id for PRIVATE message recipient",
    description=(
        "Single-field search by name or email. The caller must be inside a zone "
        "(``latitude``/``longitude`` or stored location). Returns all active owners "
        "whose profile ``zone_id`` matches the zone at that location (same pool as "
        "PANIC/PA), excluding the caller."
    ),
)
async def search_members_for_private(
    q: str = Query(default="", max_length=120, description="Optional name or email fragment"),
    latitude: float | None = Query(default=None, ge=-90, le=90),
    longitude: float | None = Query(default=None, ge=-180, le=180),
    limit: int = Query(default=20, ge=1, le=50),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    me = owner_crud.get_owner(db, current_user["user_id"])
    if not me:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    return message_feature_service.search_private_message_recipients(
        db,
        me,
        q,
        latitude=latitude,
        longitude=longitude,
        limit=limit,
    )


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
        canonical = normalize_message_type(parsed_payload.type.value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "UNSUPPORTED_MESSAGE_TYPE", "message": str(exc)},
        ) from exc
    try:
        result = message_feature_service.create_geo_propagated_message(db, sender, parsed_payload)
    except GeoMessageSkipped as skipped:
        return skipped.detail
    except (UnknownRateLimitError, SensorRateLimitError, ServicePaValidationError, ValueError) as exc:
        _handle_geo_propagation_errors(exc)
    db.commit()
    return await _finalize_geo_propagation(db, result)


class WellnessAckRequest(BaseModel):
    status: str = Field(default="ok", description="Recipient status: ok | need_help")
    note: str | None = Field(default=None, max_length=500)


class AlarmMarkReadRequest(BaseModel):
    message_ids: list[str] = Field(
        default_factory=list,
        description="Alarm message event UUIDs to mark as read for the authenticated viewer.",
    )


@router.post(
    "/messages/{message_event_id}/alarm-read",
    status_code=status.HTTP_201_CREATED,
    summary="Mark an alarm message as read",
)
async def mark_alarm_read(
    message_event_id: str = Path(..., min_length=1),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    result = alarm_read_service.record_alarm_read(
        db,
        message_event_id=message_event_id,
        owner=owner,
    )
    db.commit()
    return result


@router.post(
    "/alarms/mark-read",
    summary="Mark multiple alarm messages as read",
)
async def mark_alarms_read(
    body: AlarmMarkReadRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    result = alarm_read_service.record_alarm_reads(
        db,
        message_event_ids=body.message_ids,
        owner=owner,
    )
    db.commit()
    return result


@router.get(
    "/messages/{message_event_id}/wellness-acks",
    summary="List wellness check acknowledgements",
)
async def list_wellness_acks(
    message_event_id: str = Path(..., min_length=1),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ = current_user
    return wellness_ack_service.list_wellness_acknowledgements(
        db, message_event_id=message_event_id
    )


@router.post(
    "/messages/{message_event_id}/wellness-ack",
    status_code=status.HTTP_201_CREATED,
    summary="Acknowledge a wellness check message",
)
async def acknowledge_wellness_check(
    body: WellnessAckRequest,
    message_event_id: str = Path(..., min_length=1),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    result = wellness_ack_service.record_wellness_acknowledgement(
        db,
        message_event_id=message_event_id,
        owner=owner,
        status_value=body.status,
        note=body.note,
    )
    db.commit()
    sender_id = None
    event = db.get(ZoneMessageEvent, message_event_id)
    if event and event.sender_id:
        sender_id = event.sender_id
    notify_ids = list({int(owner.id), int(sender_id)} if sender_id else {int(owner.id)})
    await ws_manager.broadcast_to_users(notify_ids, "WELLNESS_ACK", result)
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
    except GeoMessageSkipped as skipped:
        return skipped.detail
    except (UnknownRateLimitError, SensorRateLimitError, ServicePaValidationError, ValueError) as exc:
        _handle_geo_propagation_errors(exc)
    db.commit()
    return await _finalize_geo_propagation(db, result)


@router.post("/blocks", response_model=BlockRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_block_rule(
    payload: BlockRuleCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")

    blocked_type = payload.blocked_message_type.value if payload.blocked_message_type else None
    if payload.blocked_owner_id is not None:
        blocked_member = owner_crud.get_owner(db, payload.blocked_owner_id)
        if not blocked_member:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "BLOCKED_OWNER_NOT_FOUND", "message": "Member to block was not found."},
            )
        if blocked_member.id == owner.id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error_code": "INVALID_BLOCK_SELF", "message": "You cannot block yourself."},
            )
        if blocked_member.zone_id != owner.zone_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "BLOCKED_OWNER_OUTSIDE_ZONE",
                    "message": "You can only block members in your zone.",
                },
            )

    existing = message_block_service.find_duplicate_block(
        db,
        owner_id=owner.id,
        blocked_owner_id=payload.blocked_owner_id,
        blocked_message_type=blocked_type,
    )
    if existing:
        return existing

    block = MessageBlock(
        owner_id=owner.id,
        blocked_owner_id=payload.blocked_owner_id,
        blocked_message_type=blocked_type,
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


@router.get(
    "/messages/new",
    summary="Polling fallback for new zone message events",
    description=(
        "Returns `ZoneMessageEvent` rows created at/after the **`since`** cursor. "
        "Optional **`type`** filters to a single message type (e.g. `SERVICE`) so "
        "low-priority clients can poll instead of holding a WebSocket."
    ),
)
async def list_new_feature_messages(
    since: str = Query(...),
    type: str | None = Query(
        default=None,
        description="Optional message type filter, e.g. SERVICE / PA / SENSOR.",
    ),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    viewer_id = int(current_user["user_id"])
    try:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid since cursor") from exc
    query = db.query(ZoneMessageEvent).filter(ZoneMessageEvent.created_at >= since_dt)
    if type:
        try:
            canonical = normalize_message_type(type)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error_code": "UNSUPPORTED_MESSAGE_TYPE", "message": str(exc)},
            ) from exc
        query = query.filter(ZoneMessageEvent.type == canonical.value)
    rows = (
        query
        .order_by(ZoneMessageEvent.created_at.asc())
        .limit(500)
        .all()
    )

    def _visible_to_viewer(row: ZoneMessageEvent) -> bool:
        # Mirror the inbox rule: a caller only sees events they sent, that target
        # them, or that they were a delivered recipient of. Prevents the polling
        # fallback from leaking every zone event to every authenticated user.
        if row.sender_id == viewer_id or row.receiver_id == viewer_id:
            return True
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        delivered = meta.get("delivered_owner_ids")
        return isinstance(delivered, list) and viewer_id in delivered

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
        if _visible_to_viewer(row)
    ]


@router.get(
    "/messages/private-thread",
    summary="Fetch the PRIVATE direct-message thread with another member",
    description=(
        "Returns PRIVATE-scope `ZoneMessageEvent` rows exchanged between the "
        "authenticated member and **`other_owner_id`** (both directions), oldest "
        "first. Reconstructs the private conversation thread from the event store."
    ),
)
async def get_private_thread(
    other_owner_id: int = Query(..., ge=1),
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    me = int(current_user["user_id"])
    rows = (
        db.query(ZoneMessageEvent)
        .filter(
            ZoneMessageEvent.scope == MessageScope.PRIVATE,
            or_(
                and_(
                    ZoneMessageEvent.sender_id == me,
                    ZoneMessageEvent.receiver_id == other_owner_id,
                ),
                and_(
                    ZoneMessageEvent.sender_id == other_owner_id,
                    ZoneMessageEvent.receiver_id == me,
                ),
            ),
        )
        .order_by(ZoneMessageEvent.created_at.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "type": row.type,
            "senderId": row.sender_id,
            "receiverId": row.receiver_id,
            "text": row.text,
            "body": row.body_json,
            "createdAt": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.get(
    "/emergency-events",
    summary="List recent emergency (PANIC / NS_PANIC) events",
    description=(
        "Administrator-only forensic log of MAX-priority alarms. Each row records "
        "the alarm type, sender, zone, recipient count, and origin coordinates, "
        "independent of the editable message feed."
    ),
)
async def list_emergency_events(
    limit: int = Query(100, ge=1, le=500),
    skip: int = Query(0, ge=0, le=10_000),
    type: str | None = Query(default=None, description="Optional filter: PANIC or NS_PANIC."),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    viewer = owner_crud.get_owner(db, current_user["user_id"])
    if not viewer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    if viewer.role != OwnerRole.ADMINISTRATOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "ADMIN_ONLY",
                "message": "Administrator role is required to view emergency events.",
            },
        )
    query = db.query(EmergencyEvent)
    if type:
        try:
            canonical = normalize_message_type(type)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error_code": "UNSUPPORTED_MESSAGE_TYPE", "message": str(exc)},
            ) from exc
        query = query.filter(EmergencyEvent.type == canonical.value)
    rows = (
        query.order_by(EmergencyEvent.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "messageEventId": row.message_event_id,
            "type": row.type,
            "senderId": row.sender_id,
            "zoneId": row.zone_id,
            "recipientCount": row.recipient_count,
            "latitude": row.latitude,
            "longitude": row.longitude,
            "text": row.text,
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
