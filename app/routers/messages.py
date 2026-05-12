"""Router for zone message endpoints."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.core.config import settings
from app.schemas.schemas import MessageVisibilityEnum, ZoneMessageCreate, ZoneMessageResponse
from app.crud import message as message_crud
from app.crud import owner as owner_crud
from app.core.security import get_current_user
from app.domain.message_types import (
    CanonicalMessageType,
    MessageScope,
    normalize_message_type,
    type_category,
    type_scope,
)
from app.models import GuestAccessSession, ZoneMessageEvent
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
        "**Member → guest (Access channel):** Bearer **member** JWT only. Send **`guest_id`** + **`zone_id`**/**`zoneId`**. Body: **`message`**, **`type`**/**`message_type`**, "
        "**`visibility`** (often **`private`**). Only **CHAT** is supported; **`PERMISSION`** for guests is server-only "
        "(guest arrival, **`/api/access/approve|reject`**, **guest pass** create/accept/reject/revoke via **`/api/access/guest-passes`**) — "
        "use **`422`** **`PERMISSION_MANUAL_DISABLED`** if a client sends it. "
        "Do **not** send **`receiver_id`**. Persists **`ZoneMessageEvent`**.\n\n"
        "**Responses:** **`ZoneMessageResponse`** includes **`guest_id`** (opaque guest UUID) when the event is tied to Access; "
        "**`sender_id`** = caller, **`receiver_id`** usually **`null`** for outbound member→guest **CHAT**.\n\n"
        "**Hydration:** the sending member sees this **CHAT** in **`GET /messages?owner_id={their owners.id}&skip=&limit=`** merged inbox "
        "when **`MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT`** is **true** (default). **`POST`** may also enqueue WebSocket **`NEW_MESSAGE`** "
        "with the **same payload shape** to connected staff apps (participant **`owners.id`** only).\n\n"
        "**Read path for guest:** **`GET /api/guest/messages`** with **`with_owner_id`** = caller **`owners.id`**. Admins listing the raw thread "
        "use **`GET /api/access/guest-messages`** or **`GET /messages`** + **`guest_id`** + **`zone_id`** (see **`GET /messages`**).\n\n"
        "OpenAPI schema **`ZoneMessageCreate`** includes Hex-Zone-Client examples."
    ),
    response_description=(
        "**`ZoneMessageResponse`**: integer **`id`** for **`messages`** table rows; UUID string **`id`** for **`ZoneMessageEvent`** "
        "(member→guest **CHAT**, merged inbox **PERMISSION**/**CHAT**). **`guest_id`** is set when inferable."
    ),
    responses={
        status.HTTP_201_CREATED: {
            "description": "Created **`Message`** row or **`ZoneMessageEvent`** (guest thread CHAT shows **`guest_id`**).",
            "content": {
                "application/json": {
                    "examples": {
                        "member_to_guest_chat": {
                            "summary": "Member→guest Access CHAT (UUID id + guest_id)",
                            "description": "**`POST`** with **`guest_id`** + **`zone_id`**; mirrors into merged **`GET /messages`** inbox when **`MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT=true`**.",
                            "value": {
                                "id": "019b4c72-9000-7a00-a000-acc3ss000011",
                                "zone_id": "ZN-DEMO",
                                "sender_id": 42,
                                "receiver_id": None,
                                "guest_id": "019b2c3d-0000-7000-8000-000000000001",
                                "type": "CHAT",
                                "category": "Access",
                                "scope": "private",
                                "visibility": "private",
                                "message": "Your host will meet you at reception.",
                                "created_at": "2026-05-06T15:05:00",
                            },
                        }
                    },
                },
            },
        },
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
        eid = event.id
        db.commit()
        if settings.MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT:
            zr = db.get(ZoneMessageEvent, eid)
            if zr:
                await guest_api_service.notify_access_chat_inbox_ws(zr)
        return guest_api_service.zone_message_event_to_member_zone_message_response(event)

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
        peer_rows = message_crud.list_visible_messages(
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
            for message in peer_rows
        ]

    fetch_cap = min(max(skip + limit + 128, limit * 2), 2500)
    inbox_rows = message_crud.list_visible_messages(
        db,
        owner_id=owner_id,
        other_owner_id=None,
        skip=0,
        limit=fetch_cap,
    )
    msg_responses = [
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
        for message in inbox_rows
    ]
    perm_events = guest_api_service.list_zone_permission_events_for_owner_feed(
        db,
        owner=owner,
        max_scan=fetch_cap + 250,
    )
    perm_responses = [guest_api_service.zone_message_event_to_member_zone_message_response(e) for e in perm_events]
    chat_responses: list[ZoneMessageResponse] = []
    if settings.MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT:
        chat_events = guest_api_service.list_zone_guest_access_chat_events_for_owner_inbox(
            db,
            owner=owner,
            max_scan=fetch_cap + 250,
        )
        chat_responses = [
            guest_api_service.zone_message_event_to_member_zone_message_response(e) for e in chat_events
        ]
    merged = sorted(
        [*msg_responses, *perm_responses, *chat_responses],
        key=lambda item: item.created_at,
        reverse=True,
    )
    return merged[skip : skip + limit]


@router.get(
    "",
    response_model=list[ZoneMessageResponse],
    summary="List zone messages",
    description=(
        "**Member ↔ member (no `other_owner_id`):** merges **`Message`** inbox rows with recent **`ZoneMessageEvent`** "
        "**`PERMISSION`** lines plus **Access-channel `CHAT`** for zones the caller may administer. "
        "**`PERMISSION`** includes unexpected-guest arrival/approve/deny, guest-pass arrival verification, and **guest pass** "
        "lifecycle rows (`metadata.flow` **`guest_pass_lifecycle`**, **`body.code`** one of **`GUEST_PASS_CREATED`**, "
        "**`GUEST_PASS_ACCEPTED`**, **`GUEST_PASS_REJECTED`**, **`GUEST_PASS_REVOKED`**). "
        "**`CHAT`** merge follows a **peer-party** rule aligned with **`GET /api/guest/messages`** + **`with_owner_id`**: "
        "guest→staff rows where **`receiver_id`** is the caller **`and`** the row is guest-authored; "
        "staff→guest rows where **`sender_id`** is the caller and a **`guest_id`** marker exists on the event. "
        "Disable **`CHAT`** merge server-side by setting **`MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT=false`** (env / "
        "**`.env`**); **PERMISSION** merge is unchanged.\n\n"
        "**Member ↔ member (with `other_owner_id`):** **`Message`** only between the two owners.\n\n"
        "**Guest access thread:** include any of **`guest_id`** / **`guestId`**, **`zone_id`** / **`zoneId`**, "
        "and/or **`request_id`** / **`requestId`** (**numeric **`guest_access_sessions.id`** from "
        "**`GET /api/access/guest-requests`**). **`zone_id`** may be omitted when **`guest_id`** or "
        "**`requestId`** resolves a session.\n\n"
        "Returns **`ZoneMessageEvent`** **PERMISSION** + **CHAT** (same persistence as **`GET /api/guest/messages`**). "
        "**`ZoneMessageResponse`** items may include **`guest_id`** on Access events.\n\n"
        "**Hex-Zone-Client default feed:** **`GET /messages?owner_id=`** **`<JWT sub / owners.id>`** **`&skip=0`** **`&limit=100`** "
        "(no **`guest_id`** / **`other_owner_id`**). Optional WebSocket **`NEW_MESSAGE`** uses the same normalized object as list items.\n\n"
        "**`other_owner_id`** (peer): if **`guest_id`** (or **`guestId`**) is set → same as guest **`with_owner_id`** (**dm peer**). "
        "If **`guest_id`** is omitted → **`Message`**-only thread between **`owner_id`** and **`other_owner_id`** (same zone).\n\n"
        "Optional **`GET /api/access/guest-messages`** returns the same **`ZoneMessageEvent`** items in **`{ \"data\": { \"items\": … } }`** form.\n\n"
        "This path (**`GET /messages`**, no trailing slash) remains the canonical list URL; **`GET /messages/`** "
        "is equivalent (**`include_in_schema=false`** duplicate)."
    ),
    response_description=(
        "Ordered by **`created_at`** descending after merge (**`skip`**/**`limit`** apply to the merged list). "
        "Items mirror **`ZoneMessageResponse`** (**integer `id`** vs **UUID string** — see schema **Examples**)."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "Merged inbox (**`Message`** + **`PERMISSION`** + Access **`CHAT`**) or peer-only **`Message`** list or guest **`ZoneMessageEvent`** thread.",
            "content": {
                "application/json": {
                    "examples": {
                        "merged_member_inbox": {
                            "summary": "Merged inbox: Access CHAT + PERMISSION + member messages",
                            "description": (
                                "**`GET /messages?owner_id=42&skip=0&limit=100`** shape when **`guest_id`** query params omitted: "
                                "integer‑id **`Message`** rows + UUID **`ZoneMessageEvent`** (**PERMISSION** includes guest access "
                                "arrival/approve/deny, guest-pass arrival, and **guest pass** lifecycle **`GUEST_PASS_*`**) + peer‑scoped Access **CHAT**. "
                                "Requires **`MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT=true`** for **CHAT** merge."
                            ),
                            "value": [
                                {
                                    "id": "019b4c72-9000-7a00-a000-aaaaaaaaaa01",
                                    "zone_id": "ZN-DEMO",
                                    "sender_id": None,
                                    "receiver_id": 42,
                                    "guest_id": "019b2c3d-0000-7000-8000-000000000099",
                                    "type": "CHAT",
                                    "category": "Access",
                                    "scope": "private",
                                    "visibility": "private",
                                    "message": "Hello reception",
                                    "created_at": "2026-05-06T14:31:00",
                                },
                                {
                                    "id": "019b4c72-9000-7a00-a000-dddddddd0001",
                                    "zone_id": "ZN-DEMO",
                                    "sender_id": 42,
                                    "receiver_id": None,
                                    "guest_id": "019b2c3d-0000-7000-8000-000000000099",
                                    "type": "PERMISSION",
                                    "category": "Access",
                                    "scope": "private",
                                    "visibility": "private",
                                    "message": "Guest access requested for Pat Visitor. Awaiting approval.",
                                    "created_at": "2026-05-06T14:30:00",
                                },
                                {
                                    "id": 91001,
                                    "zone_id": "ZN-DEMO",
                                    "sender_id": 40,
                                    "receiver_id": 41,
                                    "type": "PRIVATE",
                                    "category": "Alert",
                                    "scope": "private",
                                    "visibility": "private",
                                    "message": "Staff DM",
                                    "created_at": "2026-05-06T14:29:45",
                                },
                            ],
                        },
                        "guest_access_thread_scoped": {
                            "summary": "Access thread via GET /messages + guest_id (+ optional other_owner_id)",
                            "description": (
                                "**`GET /messages?owner_id=42&guest_id=…&zone_id=ZN-DEMO`** (optionally **`other_owner_id`** = peer **`owners.id`**): "
                                "**`ZoneMessageResponse[]`** only—no merge with general **`Message`** inbox."
                            ),
                            "value": [
                                {
                                    "id": "019b4c72-9000-7a00-a000-gu3st000004",
                                    "zone_id": "ZN-DEMO",
                                    "sender_id": None,
                                    "receiver_id": 42,
                                    "guest_id": "019b2c3d-0000-7000-8000-000000000001",
                                    "type": "CHAT",
                                    "category": "Access",
                                    "scope": "private",
                                    "visibility": "private",
                                    "message": "On my way",
                                    "created_at": "2026-05-06T16:10:00",
                                }
                            ],
                        },
                    }
                }
            },
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "owner_id does not match authenticated user or other_owner_id is unauthorized.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Requested owner or other_owner_id was not found.",
        },
    },
)
async def list_messages(
    owner_id: int = Query(
        ...,
        ge=1,
        description=(
            "**`owners.id`** of the authenticated member (must equal JWT **`sub`**). Matches Hex‑Zone‑Client "
            "**`GET /messages?owner_id=`** `{logged_in_owner_numeric_id}&skip=&limit=`."
        ),
    ),
    other_owner_id: int | None = Query(
        None,
        ge=1,
        description=(
            "**Peer **`owners.id`**: with **`guest_id`** → **`with_owner_id`**-style thread filter (**Access** DM). "
            "Without **`guest_id`** → member↔member **`Message`**-only transcript (same **`zone_id`** string required)."
        ),
    ),
    guest_id: str | None = Query(
        None,
        max_length=36,
        description="Opaque **`guest_access_sessions.guest_id`**. When set (or via **`guestId`**), returns raw Access thread (**`ZoneMessageEvent`**) not the merged inbox.",
    ),
    guestId: str | None = Query(None, max_length=36, description="camelCase alias of **guest_id**."),
    zone_id: str | None = Query(
        None,
        min_length=1,
        max_length=100,
        description="Required with **`guest_id`** (unless resolved from session / **`requestId`**).",
    ),
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
    skip: int = Query(0, ge=0, description="Pagination offset applied **after** server-side merge ordering."),
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description=(
            "Default **100** matches common client hydrate (**`skip=0`**). Max **1000**. "
            "Merged inbox scans a bounded recent window—use **`guest_id`**‑scoped listing for full Access history."
        ),
    ),
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
