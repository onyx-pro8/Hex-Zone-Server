"""Bearer-authenticated approved-guest APIs (`/api/guest/*`)."""

from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.core.security import get_current_guest
from app.crud import owner as owner_crud
from app.database import get_db
from app.domain.message_types import CanonicalMessageType
from app.models import GuestAccessSession, ZoneMessageEvent
from app.schemas.guest_api import (
    GuestApiHttpError,
    GuestDashboardData,
    GuestDashboardResponse,
    GuestMeData,
    GuestMeResponse,
    GuestMessageCreatedResponse,
    GuestMessagePostRequest,
    GuestMessagesListData,
    GuestMessagesListResponse,
    GuestPeerItem,
    GuestPeersData,
    GuestPeersResponse,
    GuestZoneMessageItem,
)
from app.services import guest_api_service

router = APIRouter(
    prefix="/api/guest",
    tags=["guest"],
)


def _zone_message_item(raw: dict) -> GuestZoneMessageItem:
    return GuestZoneMessageItem.model_validate(raw)


def _guest_zone_allowed(guest_ctx: dict, zone_id: str) -> None:
    z = zone_id.strip()
    allowed = {x.strip() for x in (guest_ctx.get("zone_ids") or []) if x and str(x).strip()}
    if z not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "Guest token is not valid for this zone.",
                "error_code": "ZONE_NOT_ALLOWED",
                "error": {"message": "Guest token is not valid for this zone."},
            },
        )


@router.get(
    "/me",
    response_model=GuestMeResponse,
    response_model_by_alias=True,
    summary="Guest session profile",
    description=(
        "Returns profile and JWT expiry for the **guest_access** bearer issued by "
        "**`POST /api/access/guest-session`**. Use **`Authorization: Bearer <access_token>`**."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": GuestApiHttpError, "description": "Missing/invalid token or wrong `token_use`."},
        status.HTTP_404_NOT_FOUND: {"model": GuestApiHttpError, "description": "Guest row missing (stale token)."},
    },
)
async def guest_me(
    guest_ctx: dict = Depends(get_current_guest),
    db: Session = Depends(get_db),
):
    row = (
        db.query(GuestAccessSession)
        .filter(GuestAccessSession.guest_id == guest_ctx["guest_id"])
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "Guest session not found.",
                "error_code": "NOT_FOUND",
                "error": {"message": "Guest session not found."},
            },
        )
    expires_at = guest_ctx.get("expires_at") or datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    return GuestMeResponse(
        status="success",
        data=GuestMeData(
            guest_id=row.guest_id,
            display_name=row.guest_name,
            zone_ids=guest_ctx["zone_ids"],
            allowed_message_types=["PERMISSION", "CHAT"],
            expires_at=expires_at,
        ),
    )


@router.get(
    "/zones/{zone_id}/peers",
    response_model=GuestPeersResponse,
    response_model_by_alias=True,
    summary="List zone members (peers)",
    description=(
        "Active **owners** in **`zone_id`**. **`zone_id`** must appear in the guest JWT **`zone_ids`** claim. "
        "**`can_receive_chat`** reflects whether the member has blocked **CHAT**-type delivery."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": GuestApiHttpError},
        status.HTTP_403_FORBIDDEN: {"model": GuestApiHttpError, "description": "`ZONE_NOT_ALLOWED` if zone not in token."},
    },
)
async def guest_zone_peers(
    zone_id: str = Path(
        ...,
        min_length=1,
        max_length=100,
        description="Hex zone id; must be allowed by the guest JWT.",
    ),
    guest_ctx: dict = Depends(get_current_guest),
    db: Session = Depends(get_db),
):
    zid = zone_id.strip()
    _guest_zone_allowed(guest_ctx, zid)
    peers_raw = guest_api_service.list_zone_peers_for_guest(db, zone_id=zid)
    peers = [GuestPeerItem.model_validate(p) for p in peers_raw]
    return GuestPeersResponse(status="success", data=GuestPeersData(zone_id=zid, peers=peers))


@router.get(
    "/zones/{zone_id}/dashboard",
    response_model=GuestDashboardResponse,
    response_model_by_alias=True,
    summary="Minimal zone dashboard (optional)",
    description="Safe read-only copy for guest UI: label, welcome text, links. **zone_id** must be JWT-allowed.",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": GuestApiHttpError},
        status.HTTP_403_FORBIDDEN: {"model": GuestApiHttpError},
    },
)
async def guest_zone_dashboard(
    zone_id: str = Path(..., min_length=1, max_length=100, description="Hex zone id."),
    guest_ctx: dict = Depends(get_current_guest),
    db: Session = Depends(get_db),
):
    zid = zone_id.strip()
    _guest_zone_allowed(guest_ctx, zid)
    dash = guest_api_service.get_guest_dashboard_safe(db, zone_id=zid)
    return GuestDashboardResponse(status="success", data=GuestDashboardData.model_validate(dash))


@router.get(
    "/messages",
    response_model=GuestMessagesListResponse,
    response_model_by_alias=True,
    summary="List guest-visible zone messages",
    description=(
        "Returns **PERMISSION** and **CHAT** `ZoneMessageEvent` rows involving this guest "
        "(by **`sender_guest_id`** or **`body.guest_id`**). Use **`with_owner_id`** to narrow a DM thread."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": GuestApiHttpError},
        status.HTTP_403_FORBIDDEN: {"model": GuestApiHttpError},
    },
)
async def guest_list_messages(
    guest_ctx: dict = Depends(get_current_guest),
    db: Session = Depends(get_db),
    zone_id: str = Query(
        ...,
        min_length=1,
        max_length=100,
        description="Required; must be in JWT **zone_ids**.",
    ),
    with_owner_id: int | None = Query(
        default=None,
        ge=1,
        description="If set, only messages in a thread with this **owners.id**.",
    ),
    limit: int = Query(default=50, ge=1, le=200, description="Page size (default 50, max 200)."),
    cursor: str | None = Query(
        default=None,
        max_length=500,
        description="Opaque cursor from a previous **`next_cursor`**.",
    ),
    before_id: str | None = Query(
        default=None,
        max_length=36,
        description="Optional event id: fetch messages older than this anchor.",
    ),
):
    zid = zone_id.strip()
    _guest_zone_allowed(guest_ctx, zid)
    gid = guest_ctx["guest_id"]
    before_created, before_mid = guest_api_service.parse_message_cursor(cursor)
    if before_id and before_mid is None:
        anchor = db.get(ZoneMessageEvent, before_id.strip())
        if anchor and anchor.zone_id == zid:
            before_created, before_mid = anchor.created_at, anchor.id
    rows, next_c = guest_api_service.list_guest_zone_messages(
        db,
        guest_id=gid,
        zone_id=zid,
        with_owner_id=with_owner_id,
        limit=limit,
        before_id=before_mid,
        before_created_at=before_created,
    )
    items = [_zone_message_item(guest_api_service.serialize_zone_message_for_guest(r, gid)) for r in rows]
    return GuestMessagesListResponse(
        status="success",
        data=GuestMessagesListData(items=items, next_cursor=next_c),
    )


@router.post(
    "/messages",
    response_model=GuestMessageCreatedResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Send CHAT or PERMISSION to a zone member",
    description=(
        "JSON body: **`zone_id`**, **`type`**, **`to_owner_id`**, optional **`text`** and **`msg`**. "
        "Only **CHAT** and **PERMISSION** succeed; any other **`type`** (e.g. **SERVICE**) → **403** "
        "**`forbidden_message_type`**. Recipient must be an active member of **`zone_id`** and pass block rules."
    ),
    responses={
        status.HTTP_201_CREATED: {"description": "Created message (same item shape as **GET /messages**)."},
        status.HTTP_400_BAD_REQUEST: {"model": GuestApiHttpError, "description": "Validation (missing **zone_id**, **text**, etc.)."},
        status.HTTP_401_UNAUTHORIZED: {"model": GuestApiHttpError},
        status.HTTP_403_FORBIDDEN: {"model": GuestApiHttpError, "description": "`forbidden_message_type`, blocks, or cannot receive."},
        status.HTTP_404_NOT_FOUND: {"model": GuestApiHttpError, "description": "Guest session or recipient not in zone."},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Request body failed validation (FastAPI **`detail`** array; not the **`GuestApiHttpError`** envelope)."
        },
    },
)
async def guest_post_message(
    message: GuestMessagePostRequest,
    guest_ctx: dict = Depends(get_current_guest),
    db: Session = Depends(get_db),
):
    zone_id = message.zone_id.strip()
    _guest_zone_allowed(guest_ctx, zone_id)

    msg_type = message.type.strip().upper()
    if msg_type == CanonicalMessageType.CHAT.value:
        text_s = (message.text or "").strip()
        msg_dict = None
    elif msg_type == CanonicalMessageType.PERMISSION.value:
        text_s = (message.text or "").strip() or None
        msg_dict = message.msg.model_dump(exclude_none=True) if message.msg else None
    else:
        text_s = None
        msg_dict = None

    if msg_type not in guest_api_service.GUEST_ALLOWED_TYPES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "Message type not allowed for guests.",
                "error_code": "forbidden_message_type",
                "error": {"message": "Message type not allowed for guests."},
            },
        )

    to_owner_id = message.to_owner_id

    row = (
        db.query(GuestAccessSession)
        .filter(GuestAccessSession.guest_id == guest_ctx["guest_id"])
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "Guest session not found.",
                "error_code": "NOT_FOUND",
                "error": {"message": "Guest session not found."},
            },
        )

    if msg_type == CanonicalMessageType.CHAT.value and not text_s:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "text is required for CHAT.",
                "error_code": "VALIDATION",
                "error": {"message": "text is required for CHAT."},
            },
        )

    receiver = owner_crud.get_owner(db, to_owner_id)
    if not receiver or receiver.zone_id != zone_id or not receiver.active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "Recipient is not an active member of this zone.",
                "error_code": "NOT_FOUND",
                "error": {"message": "Recipient is not an active member of this zone."},
            },
        )

    peers = guest_api_service.list_zone_peers_for_guest(db, zone_id=zone_id)
    peer = next((p for p in peers if p["owner_id"] == to_owner_id), None)
    if not peer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "Recipient is not an active member of this zone.",
                "error_code": "NOT_FOUND",
                "error": {"message": "Recipient is not an active member of this zone."},
            },
        )
    if msg_type == CanonicalMessageType.CHAT.value:
        if not peer.get("can_receive_chat"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "message": "Recipient cannot receive chat from this guest.",
                    "error_code": "FORBIDDEN",
                    "error": {"message": "Recipient cannot receive chat from this guest."},
                },
            )
    elif guest_api_service.guest_type_blocked(db, to_owner_id, CanonicalMessageType.PERMISSION.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "Recipient cannot receive permission messages from this guest.",
                "error_code": "FORBIDDEN",
                "error": {"message": "Recipient cannot receive permission messages from this guest."},
            },
        )

    created = guest_api_service.create_guest_zone_message(
        db,
        guest_id=guest_ctx["guest_id"],
        guest_display_name=row.guest_name,
        zone_id=zone_id,
        msg_type=msg_type,
        text=text_s,
        to_owner_id=to_owner_id,
        msg=msg_dict,
    )
    if created and created.get("__reject__") == "forbidden_message_type":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "Message type not allowed for guests.",
                "error_code": "forbidden_message_type",
                "error": {"message": "Message type not allowed for guests."},
            },
        )
    if created and created.get("__reject__") == "blocked":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "Recipient has blocked this message type.",
                "error_code": "FORBIDDEN",
                "error": {"message": "Recipient has blocked this message type."},
            },
        )
    if created and created.get("__reject__") == "validation":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Invalid message body.",
                "error_code": "VALIDATION",
                "error": {"message": "Invalid message body."},
            },
        )
    if not created:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Could not create message.",
                "error_code": "VALIDATION",
                "error": {"message": "Could not create message."},
            },
        )

    db.commit()
    await guest_api_service.notify_guest_message_recipient(
        recipient_owner_id=to_owner_id,
        payload={"event": created, "zone_id": zone_id},
    )
    return GuestMessageCreatedResponse(status="success", data=_zone_message_item(created))
