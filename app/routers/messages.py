"""Router for zone message endpoints."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.schemas import MessageVisibilityEnum, ZoneMessageCreate, ZoneMessageResponse
from app.crud import message as message_crud
from app.crud import owner as owner_crud
from app.core.security import get_current_user
from app.domain.message_types import CanonicalMessageType, MessageScope, normalize_message_type, type_category, type_scope
from app.services import guest_api_service
from app.services.access_policy import can_message_owner

router = APIRouter(prefix="/messages", tags=["messages"])
logger = logging.getLogger(__name__)


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
        "**`visibility`** (commonly **`private`**). Only **PERMISSION** and **CHAT** allowed with **`guest_id`**. "
        "Do **not** send **`receiver_id`**. Persists **`ZoneMessageEvent`**; guest reads via **`GET /api/guest/messages`** "
        "with **`with_owner_id`** = caller **`owners.id`**.\n\n"
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
        if guest_canonical not in (CanonicalMessageType.PERMISSION, CanonicalMessageType.CHAT):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "INVALID_MESSAGE_TYPE_FOR_GUEST",
                    "message": "Only PERMISSION and CHAT may be sent with guest_id.",
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
        "List zone-visible messages for the authenticated owner. "
        "This path (GET /messages, no trailing slash) is the canonical list URL and "
        "matches contract-style clients; GET /messages/ is equivalent and retained for "
        "backward compatibility."
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
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await _list_zone_messages_for_owner(
        owner_id=owner_id,
        other_owner_id=other_owner_id,
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
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Backward-compatible list URL (trailing slash)."""
    return await _list_zone_messages_for_owner(
        owner_id=owner_id,
        other_owner_id=other_owner_id,
        skip=skip,
        limit=limit,
        current_user=current_user,
        db=db,
    )
