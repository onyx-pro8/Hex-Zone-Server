"""Router for zone message endpoints."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.schemas import MessageVisibilityEnum, ZoneMessageCreate, ZoneMessageResponse
from app.crud import message as message_crud
from app.crud import owner as owner_crud
from app.core.security import get_current_user
from app.domain.message_types import CanonicalMessageType, normalize_message_type, type_category, type_scope
from app.models import GuestAccessSession
from app.services import guest_api_service
from app.services import guest_access_service
from app.services.access_policy import can_message_owner

router = APIRouter(prefix="/messages", tags=["messages"])
logger = logging.getLogger(__name__)


def _first_non_empty_str(*vals: str | None) -> str:
    for v in vals:
        s = (v or "").strip()
        if s:
            return s
    return ""


@router.post(
    "/",
    response_model=ZoneMessageResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="messages_create",
    summary="Create zone message or member→guest zone event",
    description=(
        "**Member ↔ member (default):** requires **`type`** (or legacy **`visibility`** only). "
        "Persists **`Message`**; **`receiver_id`** required for private-scope types.\n\n"
        "**Member → guest (Access channel):** Bearer **member** JWT only. Send **`guest_id`** (from **`GET /api/access/guest-requests`** "
        "or **`POST /api/access/permission`**) and **`zone_id`** / **`zoneId`**. Body: **`message`**, **`message_type`** or **`type`**, "
        "**`visibility`** (commonly **`private`**). Only **CHAT** is allowed with **`guest_id`**; "
        "**PERMISSION** is server-generated only. "
        "Do **not** send **`receiver_id`**. Persists **`ZoneMessageEvent`**; guest reads via **`GET /api/guest/messages`** "
        "with **`with_owner_id`** = caller **`owners.id`**. Admins list the same thread with "
        "**`GET /api/access/guest-messages`** or **`GET /messages`** + **`guest_id`** + **`zone_id`** "
        "(see **`GET /messages`** description).\n\n"
        "OpenAPI schema **`ZoneMessageCreate`** includes Hex-Zone-Client examples."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Missing or invalid bearer token.",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "**`FORBIDDEN`**: member→guest caller cannot administer **`zone_id`**, "
                "or member↔member receiver is outside allowed zone/account scope."
            ),
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "**`OWNER_NOT_FOUND`** (sender), **`RECEIVER_NOT_FOUND`**, or **`GUEST_NOT_FOUND`** (bad **guest_id** / zone).",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": (
                "Validation: missing **type** / **message_type** / legacy **visibility**, bad **receiver_id** / scope mix, "
                "**`MISSING_ZONE_FOR_GUEST`**, **`INVALID_MESSAGE_TYPE_FOR_GUEST`**, or other **`error_code`** in **detail**."
            ),
        },
    },
    response_description="**`ZoneMessageResponse`**: integer **`id`** for **`messages`**, string UUID **`id`** for guest-thread **`ZoneMessageEvent`**.",
)
async def create_message(
    payload: ZoneMessageCreate,
    response: Response,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a typed zone message with derived scope."""
    sender = owner_crud.get_owner(db, current_user["user_id"])
    if not sender:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "OWNER_NOT_FOUND", "message": "Sender owner not found"},
        )

    if (payload.guest_id or "").strip():
        gid = payload.guest_id.strip()
        zid = (payload.zone_id or "").strip()
        if not zid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "MISSING_ZONE_FOR_GUEST",
                    "message": "zone_id is required when guest_id is set.",
                },
            )
        try:
            guest_canonical = normalize_message_type(payload.type or "")
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error_code": "INVALID_MESSAGE_TYPE", "message": "Unsupported message type."},
            ) from exc
        if guest_canonical == CanonicalMessageType.PERMISSION:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "PERMISSION_MANUAL_DISABLED",
                    "message": "PERMISSION messages are system-generated only for guest workflow transitions.",
                },
            )
        if guest_canonical != CanonicalMessageType.CHAT:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "GUEST_MESSAGE_TYPE_NOT_ALLOWED",
                    "message": "Guest thread messaging supports only CHAT.",
                },
            )
        guest_result = guest_api_service.create_member_to_guest_zone_message(
            db,
            sender=sender,
            zone_id=zid,
            guest_id=gid,
            text=payload.message,
            msg_type=payload.type or "",
        )
        if isinstance(guest_result, dict):
            code = guest_result.get("__reject__")
            msg = str(guest_result.get("message") or "Request failed")
            if code == "forbidden":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error_code": "FORBIDDEN", "message": msg},
                )
            if code == "not_found":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error_code": "GUEST_NOT_FOUND", "message": msg},
                )
            if code == "permission_manual_disabled":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error_code": "PERMISSION_MANUAL_DISABLED", "message": msg},
                )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error_code": "INVALID_GUEST_MESSAGE", "message": msg},
            )
        event = guest_result
        db.commit()
        gtype = normalize_message_type(event.type)
        return ZoneMessageResponse(
            id=event.id,
            zone_id=event.zone_id,
            sender_id=event.sender_id,
            receiver_id=event.receiver_id,
            type=event.type,
            category=type_category(gtype).value,
            scope=type_scope(gtype).value,
            visibility=MessageVisibilityEnum.PRIVATE,
            message=event.text,
            created_at=event.created_at,
        )

    try:
        canonical_type = normalize_message_type(payload.type or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "INVALID_MESSAGE_TYPE", "message": "Unsupported message type."},
        ) from exc
    derived_scope = type_scope(canonical_type)

    if payload.visibility is not None and not payload.type:
        logger.warning("Deprecated legacy visibility-only payload used on /messages endpoint")
        response.headers["X-API-Deprecated"] = "visibility-only payload is deprecated; send type"

    if derived_scope == MessageScope.PRIVATE and payload.receiver_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "MISSING_RECIPIENT_FOR_PRIVATE_TYPE",
                "message": "receiver_id is required for private-scope message types.",
            },
        )

    if derived_scope == MessageScope.PUBLIC and payload.receiver_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "INVALID_TYPE_SCOPE_COMBINATION",
                "message": "receiver_id must be omitted for public-scope message types.",
            },
        )

    if payload.visibility is not None and payload.visibility.value != derived_scope.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "INVALID_VISIBILITY_FOR_TYPE",
                "message": "visibility does not match the inferred message type scope.",
            },
        )

    if payload.receiver_id is not None:
        receiver = owner_crud.get_owner(db, payload.receiver_id)
        if not receiver:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "RECEIVER_NOT_FOUND", "message": "Receiver owner not found"},
            )
        if not receiver.active:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "RECEIVER_INACTIVE",
                    "message": "Receiver account is inactive.",
                },
            )
        if receiver.id == sender.id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "INVALID_RECEIVER_SELF",
                    "message": "Sender cannot message self.",
                },
            )
        if not can_message_owner(sender, receiver, require_same_zone=True):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "RECEIVER_OUTSIDE_ALLOWED_SCOPE",
                    "message": "Receiver is outside sender account or zone scope.",
                },
            )

    db_message = message_crud.create_message(db, sender_id=sender.id, payload=payload)
    db.commit()

    return ZoneMessageResponse(
        id=db_message.id,
        zone_id=sender.zone_id,
        sender_id=db_message.sender_id,
        receiver_id=db_message.receiver_id,
        type=db_message.message_type,
        category=type_category(canonical_type).value,
        scope=derived_scope.value,
        visibility=db_message.visibility,
        message=db_message.message,
        created_at=db_message.created_at,
    )


async def _list_zone_messages_for_owner(
    *,
    owner_id: int,
    other_owner_id: int | None,
    guest_id: str | None,
    zone_id: str | None,
    guest_id_camel: str | None,
    zone_id_camel: str | None,
    permission_request_id: str | None,
    request_id: str | None,
    request_id_camel: str | None,
    access_session_id: str | None,
    skip: int,
    limit: int,
    current_user: dict,
    db: Session,
) -> list[ZoneMessageResponse]:
    if owner_id != current_user["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: owner_id must match authenticated user",
        )

    owner = owner_crud.get_owner(db, owner_id)
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )

    gid = _first_non_empty_str(guest_id, guest_id_camel, permission_request_id)
    zid = _first_non_empty_str(zone_id, zone_id_camel)
    req_hint = _first_non_empty_str(request_id, request_id_camel, access_session_id)

    # Clients often send **`requestId`** = numeric **`guest_access_sessions.id`** from
    # **`GET /api/access/guest-requests`**, not the opaque **guest_id** UUID (camelCase **`guestId`**
    # **`zoneId`** are accepted too).
    if not gid and req_hint:
        if req_hint.isdigit():
            sess_by_pk = db.get(GuestAccessSession, int(req_hint))
            if not sess_by_pk:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error_code": "GUEST_NOT_FOUND",
                        "message": "Guest access session not found for this request id.",
                    },
                )
            if not guest_access_service.can_manage_zone_guest_requests(db, owner, sess_by_pk.zone_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error_code": "FORBIDDEN",
                        "message": "You are not allowed to view guest access thread for this zone.",
                    },
                )
            gid = sess_by_pk.guest_id
            zid = zid or sess_by_pk.zone_id
        else:
            gid = req_hint

    if gid and not zid:
        sess = guest_access_service.get_guest_access_session_by_guest_id(db, gid)
        if not sess:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "GUEST_NOT_FOUND",
                    "message": "Guest session not found for this zone.",
                },
            )
        zid = sess.zone_id

    if gid:
        if not zid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "MISSING_ZONE_FOR_GUEST",
                    "message": "zone_id is required when guest_id is provided.",
                },
            )
        if not guest_access_service.can_manage_zone_guest_requests(db, owner, zid):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "FORBIDDEN",
                    "message": "You are not allowed to view guest access thread for this zone.",
                },
            )
        row = guest_access_service.get_guest_access_session_by_guest_id(db, gid)
        if not row or row.zone_id != zid:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "GUEST_NOT_FOUND",
                    "message": "Guest session not found for this zone.",
                },
            )
        rows = guest_api_service.list_guest_access_thread_for_zone_member(
            db,
            zone_id=zid,
            guest_id=gid,
            peer_owner_id=other_owner_id,
            skip=skip,
            limit=limit,
        )
        return [
            guest_api_service.zone_message_event_to_member_zone_message_response(event) for event in rows
        ]

    if other_owner_id is not None:
        other_owner = owner_crud.get_owner(db, other_owner_id)
        if not other_owner:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="other_owner_id not found",
            )
        if other_owner.zone_id != owner.zone_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="other_owner_id is not in the same zone",
            )

    rows = message_crud.list_visible_messages(
        db,
        owner_id=owner_id,
        other_owner_id=other_owner_id,
        skip=skip,
        limit=limit,
    )
    return [
        ZoneMessageResponse(
            id=message.id,
            zone_id=owner.zone_id,
            sender_id=message.sender_id,
            receiver_id=message.receiver_id,
            type=message.message_type,
            category=type_category(normalize_message_type(message.message_type)).value,
            scope=type_scope(normalize_message_type(message.message_type)).value,
            visibility=message.visibility,
            message=message.message,
            created_at=message.created_at,
        )
        for message in rows
    ]


@router.get(
    "",
    response_model=list[ZoneMessageResponse],
    summary="List zone messages",
    description=(
        "**Member ↔ member:** returns **`Message`** rows for **`owner_id`** (+ optional **`other_owner_id`**).\n\n"
        "**Guest access thread:** include any of **`guest_id`** / **`guestId`**, **`zone_id`** / **`zoneId`**, "
        "and/or **`request_id`** / **`requestId`** (**numeric **`guest_access_sessions.id`** from "
        "**`GET /api/access/guest-requests`**). **`zone_id`** may be omitted when **`guest_id`** or "
        "**`requestId`** resolves a session.\n\n"
        "Returns **`ZoneMessageEvent`** **PERMISSION** + **CHAT** (same persistence as **`GET /api/guest/messages`**). "
        "Optional **`GET /api/access/guest-messages`** returns the same items in **`{ \"data\": { \"items\": … } }`** form.\n\n"
        "This path (**`GET /messages`**, no trailing slash) remains the canonical list URL; **`GET /messages/`** "
        "is equivalent."
    ),
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "owner_id does not match authenticated user or other_owner_id is unauthorized.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Requested owner or other_owner_id was not found.",
        },
    },
    response_description="Caller-visible message list in zone scope.",
)
async def list_messages(
    owner_id: int = Query(..., ge=1),
    other_owner_id: int | None = Query(None, ge=1),
    guest_id: str | None = Query(None, max_length=36),
    guestId: str | None = Query(None, max_length=36, description="camelCase alias of **guest_id**."),
    zone_id: str | None = Query(None, min_length=1, max_length=100),
    zoneId: str | None = Query(None, min_length=1, max_length=100, description="camelCase alias of **zone_id**."),
    permission_request_id: str | None = Query(
        None,
        max_length=36,
        description="Usually same opaque id as **`guest_id`** when clients use alternate naming.",
    ),
    request_id: str | None = Query(None, max_length=36),
    requestId: str | None = Query(
        None,
        max_length=36,
        description="Often **numeric **`guest_access_sessions.id`** OR opaque **guest_id** UUID**.",
    ),
    access_session_id: str | None = Query(None, max_length=36, description="Alias of **`request_id`**."),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await _list_zone_messages_for_owner(
        owner_id=owner_id,
        other_owner_id=other_owner_id,
        guest_id=guest_id,
        zone_id=zone_id,
        guest_id_camel=guestId,
        zone_id_camel=zoneId,
        permission_request_id=permission_request_id,
        request_id=request_id,
        request_id_camel=requestId,
        access_session_id=access_session_id,
        skip=skip,
        limit=limit,
        current_user=current_user,
        db=db,
    )


@router.get(
    "/",
    response_model=list[ZoneMessageResponse],
    include_in_schema=False,
)
async def list_messages_trailing_slash(
    owner_id: int = Query(..., ge=1),
    other_owner_id: int | None = Query(None, ge=1),
    guest_id: str | None = Query(None, max_length=36),
    guestId: str | None = Query(None, max_length=36),
    zone_id: str | None = Query(None, min_length=1, max_length=100),
    zoneId: str | None = Query(None, min_length=1, max_length=100),
    permission_request_id: str | None = Query(None, max_length=36),
    request_id: str | None = Query(None, max_length=36),
    requestId: str | None = Query(None, max_length=36),
    access_session_id: str | None = Query(None, max_length=36),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Backward-compatible list URL (trailing slash)."""
    return await _list_zone_messages_for_owner(
        owner_id=owner_id,
        other_owner_id=other_owner_id,
        guest_id=guest_id,
        zone_id=zone_id,
        guest_id_camel=guestId,
        zone_id_camel=zoneId,
        permission_request_id=permission_request_id,
        request_id=request_id,
        request_id_camel=requestId,
        access_session_id=access_session_id,
        skip=skip,
        limit=limit,
        current_user=current_user,
        db=db,
    )
