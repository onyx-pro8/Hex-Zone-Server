"""Bearer-authenticated approved-guest APIs (`/api/guest/*`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decode_guest_access_bearer, security
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
from pydantic import ValidationError

from app.schemas.message_feature import PropagationMessageCreate, PropagationMessageResponse
from app.routers.message_feature import _finalize_geo_propagation, _handle_geo_propagation_errors
from app.services.message_feature_service import (
    GeoMessageSkipped,
    UnknownRateLimitError,
    SensorRateLimitError,
    create_network_guest_geo_propagated_message,
    search_network_guest_private_message_recipients,
)
from app.domain.service_pa_topics import ServicePaValidationError
from app.services import guest_api_service, guest_access_service

# OpenAPI: shared **401** documentation for all **`/api/guest/*`** routes (dependency runs before handler body).
_GUEST_API_UNAUTHORIZED: dict[str, Any] = {
    "model": GuestApiHttpError,
    "description": (
        "Missing **`Authorization`**, unreadable JWT, or **`exp`** in the past (handler may use **`error_code`** "
        "**`HTTP_401`** when **`detail`** was a plain string from legacy **`verify_token`**). "
        "Wrong guest token shape → **`INVALID_GUEST_TOKEN`**. "
        "Server-side loss of access while JWT is still within **`exp`** → **`GUEST_ACCESS_INVALIDATED`** "
        "(revoked/denied **`guest_access_sessions`**, revoked/rejected/expired consumed **guest pass**, or revoked "
        "**`guest_access_qr_tokens`** row linked to the session). **Client:** clear stored guest token and return to "
        "**`POST /api/access/permission`** → poll **`GET /api/access/session/{guest_id}`** → **`POST /api/access/guest-session`**."
    ),
}

router = APIRouter(
    prefix="/api/guest",
    tags=["guest"],
)


async def get_current_guest(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> dict:
    """Guest JWT + live **`guest_access_sessions`** / guest-pass / QR revocation check."""
    ctx = decode_guest_access_bearer(credentials)
    guest_access_service.require_guest_bearer_session_active(db, guest_id=ctx["guest_id"])
    return ctx


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
                "error_code": "GUEST_NOT_AUTHORIZED_FOR_ZONE",
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
        "**`POST /api/access/guest-session`**. Use **`Authorization: Bearer <access_token>`**. "
        "Each call re-checks the database (session row, guest pass, QR token); revoked access fails here first. "
        "**`allowed_message_types`** mirrors the JWT claim (defaults to CHAT-only)."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: _GUEST_API_UNAUTHORIZED,
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
    amt_raw = guest_ctx.get("allowed_message_types") or []
    allowed: list[str] = []
    for t in amt_raw:
        u = str(t).strip().upper()
        if u and u not in allowed:
            allowed.append(u)
    if not allowed:
        allowed = ["CHAT"]
    return GuestMeResponse(
        status="success",
        data=GuestMeData(
            guest_id=row.guest_id,
            display_name=row.guest_name,
            zone_ids=guest_ctx["zone_ids"],
            allowed_message_types=allowed,
            expires_at=expires_at,
        ),
    )


@router.get(
    "/zones/{zone_id}/peers",
    response_model=GuestPeersResponse,
    response_model_by_alias=True,
    summary="List zone members (peers)",
    description=(
        "**Staff peers only** (not every user in the zone): active **ADMINISTRATOR** owners with matching **`owners.zone_id`**, "
        "**`zones.owner_id`** for active zone rows, plus the **primary zone admin** fallback. "
        "**`zone_id`** must appear in the JWT **`zone_ids`** claim. Each **`owner_id`** is **`owners.id`** — use as "
        "**`with_owner_id`** (GET thread) or **`to_owner_id`** (POST CHAT). **`can_receive_chat`** is false when the member blocked **CHAT**.\n\n"
        "Same **`401`** semantics as **`GET /api/guest/me`** (see **`GuestApiHttpError`** example **`GUEST_ACCESS_INVALIDATED`**)."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: _GUEST_API_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: {"model": GuestApiHttpError, "description": "`GUEST_NOT_AUTHORIZED_FOR_ZONE` if zone not in token."},
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
    summary="Guest zone dashboard + map hints (optional)",
    description=(
        "Read-only guest UI payload: **`label`**, **`welcome_text`**, **`links`**, **`cells`** (H3 from **`zones.h3_cells`**), "
        "and **`map`** (`center`, `zoom`, `cells`, optional `bounds` / `geojson` from **`zones.parameters.guest_map`**). "
        "**`zone_id`** must be JWT-allowed. See Swagger **Example** on **`GuestDashboardResponse`**.\n\n"
        "Same **`401`** semantics as **`GET /api/guest/me`**."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: _GUEST_API_UNAUTHORIZED,
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
        "Returns **`ZoneMessageEvent`** rows: **PERMISSION** (server-only: submit / approve / reject) and **CHAT** (guest or member). "
        "Query **`zone_id`** (required, must be in JWT) and optional **`with_owner_id`** = staff **`owners.id`**. "
        "**PERMISSION** audit lines appear for **every** **`with_owner_id`** thread (canonical guest↔staff view). "
        "**CHAT** lines are peer-scoped the same way staff **`GET /messages`** merged inbox uses **party** filters for Access **CHAT**.\n\n"
        "**Pagination:** **`limit`**, **`cursor`** (opaque), optional **`before_id`**. See **`GuestMessagesListResponse`** example.\n\n"
        "Same **`401`** semantics as **`GET /api/guest/me`**."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: _GUEST_API_UNAUTHORIZED,
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
        description="Staff **`owners.id`** from **`GET …/peers`**. Narrows **CHAT** DM; **PERMISSION** rows remain visible for this peer context.",
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
    summary="Send CHAT to a zone member",
    description=(
        "JSON body: **`zone_id`**, **`type`**, **`to_owner_id`**, and **`text`**. "
        "Only **CHAT** is allowed. **PERMISSION** and other types → **`422`** with **`PERMISSION_MANUAL_DISABLED`** or "
        "**`GUEST_MESSAGE_TYPE_NOT_ALLOWED`**. Recipient must be **`GET …/peers`** staff: wrong target → **`403`** **`GUEST_NOT_AUTHORIZED_FOR_ZONE`** "
        "(or **`PEERS_NOT_AVAILABLE`** if no peers).\n\n"
        "**Side effects:** persists **`ZoneMessageEvent`** (**`guest_id`** in body/metadata). Recipient staff see the same chat in **`GET /messages?owner_id={to_owner_id}`** merged "
        "inbox (**`skip`/`limit`**) when **`MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT`** is **true** (default)—**`ZoneMessageResponse`** mirrors **`guest_id`**, **`type`:** **`CHAT`**. "
        "Optional **`NEW_MESSAGE`** WebSocket (**same JSON shape**) to participant **`owners.id`**. See **`GuestMessageCreatedResponse`** example.\n\n"
        "Same **`401`** semantics as **`GET /api/guest/me`**."
    ),
    responses={
        status.HTTP_201_CREATED: {"description": "Created **CHAT** zone event (**`GuestZoneMessageItem`**)."},
        status.HTTP_400_BAD_REQUEST: {
            "model": GuestApiHttpError,
            "description": "**`VALIDATION`** (missing **text**, bad body).",
        },
        status.HTTP_401_UNAUTHORIZED: _GUEST_API_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: {
            "model": GuestApiHttpError,
            "description": "**`GUEST_NOT_AUTHORIZED_FOR_ZONE`**, **`PEERS_NOT_AVAILABLE`**, **`FORBIDDEN`** (chat block), …",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": GuestApiHttpError,
            "description": "**`NOT_FOUND`** — unknown guest session or inactive recipient.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "**`PERMISSION_MANUAL_DISABLED`** or **`GUEST_MESSAGE_TYPE_NOT_ALLOWED`**.",
            "model": GuestApiHttpError,
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
    if msg_type == CanonicalMessageType.PERMISSION.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "PERMISSION messages are system-generated only for guest workflow transitions.",
                "error_code": "PERMISSION_MANUAL_DISABLED",
                "error": {"message": "PERMISSION messages are system-generated only for guest workflow transitions."},
            },
        )
    if msg_type != CanonicalMessageType.CHAT.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Guest message type is not allowed.",
                "error_code": "GUEST_MESSAGE_TYPE_NOT_ALLOWED",
                "error": {"message": "Guest message type is not allowed."},
            },
        )
    text_s = (message.text or "").strip()
    msg_dict = None

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
    if not receiver or not receiver.active:
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
        err_code = "PEERS_NOT_AVAILABLE" if len(peers) == 0 else "GUEST_NOT_AUTHORIZED_FOR_ZONE"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": (
                    "No zone staff are configured for messaging."
                    if err_code == "PEERS_NOT_AVAILABLE"
                    else "Recipient is not an authorized host/admin peer for this zone."
                ),
                "error_code": err_code,
                "error": {
                    "message": (
                        "No zone staff are configured for messaging."
                        if err_code == "PEERS_NOT_AVAILABLE"
                        else "Recipient is not an authorized host/admin peer for this zone."
                    ),
                },
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
                "error_code": "GUEST_MESSAGE_TYPE_NOT_ALLOWED",
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
    if created and created.get("__reject__") == "forbidden":
        msg = str(created.get("message") or "Recipient is not an authorized peer for this zone.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": msg,
                "error_code": "GUEST_NOT_AUTHORIZED_FOR_ZONE",
                "error": {"message": msg},
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
    mid = str(created.get("id") or "").strip()
    if mid and settings.MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT:
        zr = db.get(ZoneMessageEvent, mid)
        if zr:
            await guest_api_service.notify_access_chat_inbox_ws(db, zr)
    await guest_api_service.notify_guest_message_recipient(
        recipient_owner_id=to_owner_id,
        payload={"event": created, "zone_id": zone_id},
    )
    return GuestMessageCreatedResponse(status="success", data=_zone_message_item(created))


@router.get(
    "/messages/members/search",
    summary="Search network members for PRIVATE compose (network-access guest)",
    description=(
        "Requires **network access** guest session. Uses the guest's current device coordinates "
        "and the network id from the QR scan to apply the same zone gate as invited members."
    ),
    responses={status.HTTP_401_UNAUTHORIZED: _GUEST_API_UNAUTHORIZED},
)
async def guest_search_private_recipients(
    q: str = Query(default="", max_length=120, description="Optional name or email fragment"),
    latitude: float | None = Query(default=None, ge=-90, le=90),
    longitude: float | None = Query(default=None, ge=-180, le=180),
    limit: int = Query(default=20, ge=1, le=50),
    guest_ctx: dict = Depends(get_current_guest),
    db: Session = Depends(get_db),
):
    if not guest_ctx.get("network_geo_messaging"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "PRIVATE search requires a network access guest session.",
                "error_code": "GUEST_GEO_NOT_ALLOWED",
            },
        )
    row = (
        db.query(GuestAccessSession)
        .filter(GuestAccessSession.guest_id == guest_ctx["guest_id"])
        .first()
    )
    if not row or not guest_access_service.session_allows_network_geo_messaging(row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "PRIVATE search requires a network access guest session.",
                "error_code": "GUEST_GEO_NOT_ALLOWED",
            },
        )
    return search_network_guest_private_message_recipients(
        db,
        guest_session=row,
        query=q,
        latitude=latitude,
        longitude=longitude,
        limit=limit,
    )


@router.post(
    "/messages/propagate",
    response_model=PropagationMessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Send geo-propagated alarm/alert (network access guest)",
    description=(
        "Requires **network access** guest session (**`kind`** = **`network_access`**) after scanning a "
        "network QR (**`/access?nid=`** or **`gt`+`nid`**). Sends PANIC, NS-PANIC, UNKNOWN, PRIVATE, PA, or SERVICE "
        "using primary vs secondary acceptable-zone routing for the network."
    ),
    responses={status.HTTP_401_UNAUTHORIZED: _GUEST_API_UNAUTHORIZED},
)
async def guest_propagate_message(
    payload: dict,
    guest_ctx: dict = Depends(get_current_guest),
    db: Session = Depends(get_db),
):
    if not guest_ctx.get("network_geo_messaging"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "Geo propagation requires a network access guest session.",
                "error_code": "GUEST_GEO_NOT_ALLOWED",
                "error": {"message": "Geo propagation requires a network access guest session."},
            },
        )

    msg_type = str(payload.get("type") or "").strip().upper()
    if msg_type in ("PERMISSION", "SENSOR", "WELLNESS CHECK", "WELLNESS_CHECK"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "GUEST_MESSAGE_TYPE_NOT_ALLOWED",
                "message": f"Guests may not send {msg_type} via geo propagation.",
            },
        )

    allowed = {str(t).strip().upper() for t in (guest_ctx.get("allowed_message_types") or [])}
    if msg_type.replace("-", "_") not in {a.replace("-", "_") for a in allowed} and msg_type not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "GUEST_MESSAGE_TYPE_NOT_ALLOWED",
                "message": "Guest message type is not allowed.",
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

    row = (
        db.query(GuestAccessSession)
        .filter(GuestAccessSession.guest_id == guest_ctx["guest_id"])
        .first()
    )
    if not row or not guest_access_service.session_allows_network_geo_messaging(row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "Geo propagation requires a network access guest session.",
                "error_code": "GUEST_GEO_NOT_ALLOWED",
            },
        )

    try:
        result = create_network_guest_geo_propagated_message(db, guest_session=row, payload=parsed_payload)
    except GeoMessageSkipped as skipped:
        return skipped.detail
    except (UnknownRateLimitError, SensorRateLimitError, ServicePaValidationError, ValueError) as exc:
        _handle_geo_propagation_errors(exc)
    db.commit()
    return await _finalize_geo_propagation(db, result)
