"""Public QR guest access and administrator approve/reject."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.guest_permission_rate_limit import allow_request
from app.core.security import create_guest_access_token, get_current_user
from app.crud import owner as owner_crud
from app.database import get_db
from app.models import GuestAccessSession
from app.models.owner import Owner, OwnerRole
from app.schemas.access_guest import (
    AccessPermissionResponseData,
    AccessPermissionResponseEnvelope,
    AccessSessionPollData,
    AccessSessionPollEnvelope,
    GuestAccessHttpError,
    GuestAccessQrLinkResponse,
    GuestAccessSessionListItem,
    GuestAdminDecisionResponse,
    GuestArrivalMessagesData,
    GuestArrivalMessagesDefaults,
    GuestArrivalMessagesEnvelope,
    GuestArrivalMessagesPatch,
    GuestArrivalRequest,
    GuestQrTokenCreate,
    GuestRequestDecisionData,
    GuestRequestDecisionEnvelope,
    GuestRequestListContractEnvelope,
    GuestRequestListItemContract,
    PrimaryGuestQrRotateRequest,
    PrimaryGuestQrTokenData,
    PrimaryGuestQrTokenEnvelope,
    GuestQrTokenCreatedResponse,
    GuestQrTokenLinkBundle,
    GuestQrTokenListItem,
    GuestRequestListEnvelope,
    GuestScanResponse,
    GuestSessionExchangeRequest,
    GuestSessionPollResponse,
    GuestZoneActionRequest,
)
from app.schemas.guest_pass import (
    GuestPassAdminRequest,
    GuestPassCreateRequest,
    GuestPassCreatedData,
    GuestPassCreatedEnvelope,
    GuestPassDecisionData,
    GuestPassDecisionEnvelope,
    GuestPassListEnvelope,
    GuestPassListItem,
)
from app.schemas.guest_api import (
    GuestApiHttpError,
    GuestSessionExchangeData,
    GuestSessionExchangeResponse,
    GuestSessionGuestProfile,
)
from app.schemas.schemas import MemberGuestAccessThreadMessagesData, MemberGuestAccessThreadMessagesEnvelope
from app.services import (
    guest_access_qr,
    guest_access_qr_token_service,
    guest_access_service,
    guest_api_service,
    guest_arrival_zone_messages as guest_arrival_zone_messages_service,
    guest_pass_service,
)
from app.websocket.manager import ws_manager

logger = logging.getLogger(__name__)
_GUEST_NAME_ALLOWED_RE = re.compile(r"^[A-Za-z0-9 .,'-]+$")

router = APIRouter(prefix="/api/access", tags=["access"])

_PERM_SUMMARY = "Guest arrival (QR scan)"
_PERM_DESCRIPTION = """
No authentication. Supply either **`zone_id`** (static SPA / QR: **`/access?zid=`**) or **`guest_qr_token`**
(opaque **`gt`** from an issued invite). Server-mint links use **`/access?gt=…&zid=…`** (optional **`eid`** when the token binds an event);
legacy **`/access?gt=…`** without **`zid`** is still accepted in the SPA, but **`zone_id`** in this body is omitted in that case.

Validates that **zone_id** exists (explicit or resolved from token), then resolves the arrival in priority order:

1. **Guest Pass match** (new): when **`event_id`** is submitted, the server looks up
   **`guest_passes`** where `zone_id` + **`event_id`** match an **ACCEPTED**,
   non-expired, unconsumed pass. Matching uses the same **canonical event id** rules as schedules
   (trim; Unicode case-insensitive for general ids; **`EVT-1234`**, **`evt_1234`**, **`EVT1234`**, and **`1234`**
   are treated as the same numeric event when the suffix is all digits). If found, the pass is consumed
   (`used_by_guest_id` is set),
   the guest is auto-approved as **EXPECTED**, and a `guest_is_here` WebSocket event is
   broadcast to all zone members. Guest passes are created via **`POST /api/access/guest-passes`**
   and accepted by an admin via **`POST /api/access/guest-passes/{id}/accept`**.
2. **Access Schedule match**: finds an active schedule whose time window contains server time
   and matches **`event_id`** (canonical rules above) or **`guest_name`**.
3. **No match → UNEXPECTED**: persist a pending session, push WebSocket **`unexpected_guest`**
   to all active owners sharing **zone_id**, and record a PERMISSION zone event.

Outcomes:

- **EXPECTED** (schedule or guest pass match): persist a guest session, write a PERMISSION zone event,
  push WebSocket **`guest_is_here`** to the schedule creator / zone members.
- **UNEXPECTED**: persist a pending session, push WebSocket **`unexpected_guest`** to all active owners
  sharing **zone_id**, and record a PERMISSION zone event for request/decision history.

Those **PERMISSION** rows live in **`zone_message_events`**. **Guest-facing arrival strings** (expected schedule,
unexpected pending, guest pass verified) may be overridden per zone via
**`GET` / `PATCH` / `PUT /api/access/zones/{zone_id}/guest-arrival-messages`**; each successful arrival **snapshots**
the effective **`message`** on **`guest_access_sessions`** and embeds the same value as **`guest_message`** on the
**PERMISSION** event so staff and guest stay aligned. **Guest pass** create/accept/reject/revoke also appends
**`PERMISSION`** rows (`metadata.flow` **`guest_pass_lifecycle`**, **`body.code`** one of **`GUEST_PASS_*`**).
Eligible owners see merged **`PERMISSION`** lines on **`GET /messages?owner_id=`**; guest-facing threads use
**`GET /api/guest/messages`** / **`GET /api/access/guest-messages`** where a session or guest id applies.

**Response** includes **`guest_id`** (poll path) and **`zone_id`** (use as **`zone_id`** query on
`GET /api/access/session/{guest_id}` when the client does not already have **`zid`** from the URL).
Session polling also succeeds with **`guest_id`** only (**`zone_id`** query omitted).

Backend-issued tokens (`POST /api/access/qr-tokens`) may enforce expiry, revocation, and optional **max_uses** (counted on successful arrivals only).

Rate-limited per client IP (rolling minute). CORS: browser guests should call the API from an allowed origin (this server enables permissive CORS by default).

**`event_id`:** clients may send the invite **`eid`** query string unchanged (aside from trim). The server normalizes
**`EVT-…`** numeric event codes and bare digit strings to one logical key for guest-pass and schedule matching; other
ids are compared case-insensitively. Prefer using the same string you store on schedules / guest passes / QR **`eid`**;
equivalent forms still match.
"""


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _sanitize_guest_name_or_422(raw_name: str) -> str:
    normalized = " ".join((raw_name or "").strip().split())
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "INVALID_GUEST_NAME", "message": "guest_name is required."},
        )
    if len(normalized) > 255:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "INVALID_GUEST_NAME", "message": "guest_name is too long."},
        )
    if not _GUEST_NAME_ALLOWED_RE.match(normalized):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "INVALID_GUEST_NAME", "message": "guest_name contains unsupported characters."},
        )
    return normalized


def _require_guest_qr_administrator(db: Session, current_user: dict, zone_id: str) -> Owner:
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    zid = zone_id.strip()
    if owner.zone_id != zid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "FORBIDDEN",
                "message": "You may only request guest QR links for your own zone.",
            },
        )
    if owner.role != OwnerRole.ADMINISTRATOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "FORBIDDEN",
                "message": "Administrator role required to fetch guest access QR material.",
            },
        )
    return owner


def _require_zone_guest_arrival_messages_admin(db: Session, current_user: dict, zone_id: str) -> Owner:
    """Same cohort as **GET /api/access/guest-requests** / guest thread admin views."""
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Owner not found"},
        )
    zid = zone_id.strip()
    if owner.role != OwnerRole.ADMINISTRATOR or owner.zone_id != zid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "FORBIDDEN",
                "message": "Administrator role is required for this zone.",
            },
        )
    if not guest_access_service.can_manage_zone_guest_requests(db, owner, zid):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "FORBIDDEN",
                "message": "You are not allowed to administer guest arrival messages for this zone.",
            },
        )
    return owner


def _primary_token_contract_payload(row) -> PrimaryGuestQrTokenData:
    path = guest_access_qr.guest_access_path_with_guest_token(row.token, zone_id=row.zone_id, event_id=None)
    url = guest_access_qr.guest_access_absolute_url_with_guest_token(row.token, zone_id=row.zone_id, event_id=None)
    return PrimaryGuestQrTokenData(
        id=row.id,
        zone_id=row.zone_id,
        token_suffix=row.token[-6:] if len(row.token) >= 6 else row.token,
        url=url,
        path_with_query=path,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# Guest Pass endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/guest-passes",
    response_model=GuestPassCreatedEnvelope,
    status_code=status.HTTP_201_CREATED,
    operation_id="access_create_guest_pass",
    summary="Create a guest pass request",
    description=(
        "**Bearer** member JWT. Any authenticated zone member can pre-register an expected guest by "
        "submitting a guest pass with a unique **`event_id`** and an **`expires_at`** datetime.\n\n"
        "The pass is created in **PENDING** status. A zone administrator must accept it "
        "(**`POST /api/access/guest-passes/{id}/accept`**) before it becomes active for auto-approval.\n\n"
        "When a guest later arrives at **`POST /api/access/permission`** with the same **`event_id`**, "
        "the server automatically approves the guest if a valid accepted pass exists.\n\n"
        "**Side effects** (same DB transaction as this response): a **`ZoneMessageEvent`** row with **`type`** "
        "**`PERMISSION`** is persisted (`metadata.flow` = **`guest_pass_lifecycle`**, **`body.code`** = **`GUEST_PASS_CREATED`**). "
        "Callers who receive merged guest-access **`PERMISSION`** lines will see it on **`GET /messages?owner_id=...`**.\n\n"
        "A **`PERMISSION_MESSAGE`** WebSocket payload (same codes and copy) is also sent to **zone staff** "
        "(see `delivered_owner_ids` in the server implementation)."
    ),
    response_description="Created guest pass with PENDING status.",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not a member of the specified zone (**FORBIDDEN**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Owner not found or unknown zone (**INVALID_ZONE**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_409_CONFLICT: {
            "description": "**`event_id`** already exists for this zone (**DUPLICATE_EVENT_ID**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "**`expires_at`** is in the past (**INVALID_EXPIRY**) or validation failure.",
            "model": GuestAccessHttpError,
        },
    },
)
async def create_guest_pass(
    payload: GuestPassCreateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error_code": "NOT_FOUND", "message": "Owner not found"})

    result = guest_pass_service.create_guest_pass(
        db,
        owner=owner,
        zone_id=payload.zone_id,
        event_id=payload.event_id,
        guest_name=payload.guest_name,
        notes=payload.notes,
        expires_at=payload.expires_at,
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )

    row = result["row"]
    db.commit()
    db.refresh(row)

    ws_payload = guest_pass_service.build_guest_pass_ws_payload(
        db, guest_pass=row, code="GUEST_PASS_CREATED", zone_id=row.zone_id,
    )
    member_ids = ws_payload["data"]["delivered_owner_ids"]
    if member_ids:
        await ws_manager.broadcast_to_users(member_ids, "PERMISSION_MESSAGE", ws_payload["data"])

    return GuestPassCreatedEnvelope(
        data=GuestPassCreatedData(
            id=row.id,
            zone_id=row.zone_id,
            event_id=row.event_id,
            guest_name=row.guest_name,
            notes=row.notes,
            status="PENDING",
            requested_by=row.requested_by,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )
    )


@router.get(
    "/guest-passes",
    response_model=GuestPassListEnvelope,
    status_code=status.HTTP_200_OK,
    operation_id="access_list_guest_passes",
    summary="List guest passes for a zone",
    description=(
        "**Bearer** member JWT. Returns all guest passes for the specified **`zone_id`**, "
        "sorted by **`created_at`** descending (newest first).\n\n"
        "Each item includes a computed **`is_expired`** boolean (`true` when `now > expires_at`) "
        "and **`requested_by_name`** (display name of the member who created the pass).\n\n"
        "**Query params:**\n"
        "- **`zone_id`** (required): the zone to list passes for.\n"
        "- **`status`** (optional): filter by **PENDING**, **ACCEPTED**, **REJECTED**, **REVOKED**, or **ALL** (default)."
    ),
    response_description="List of guest passes for the zone.",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not a member of the specified zone (**FORBIDDEN**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Owner not found.",
            "model": GuestAccessHttpError,
        },
    },
)
async def list_guest_passes(
    zone_id: str = Query(..., min_length=1, max_length=100, description="Hex zone id; required."),
    filter_status: str | None = Query(
        default=None,
        alias="status",
        max_length=16,
        description="Filter: **PENDING**, **ACCEPTED**, **REJECTED**, **REVOKED**, or **ALL** (default).",
    ),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error_code": "NOT_FOUND", "message": "Owner not found"})

    result = guest_pass_service.list_guest_passes(
        db, owner=owner, zone_id=zone_id, status_filter=filter_status,
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )

    items = [GuestPassListItem.model_validate(i) for i in result["items"]]
    return GuestPassListEnvelope(data=items)


@router.post(
    "/guest-passes/{pass_id}/accept",
    response_model=GuestPassDecisionEnvelope,
    status_code=status.HTTP_200_OK,
    operation_id="access_accept_guest_pass",
    summary="Accept a guest pass (admin only)",
    description=(
        "**Bearer** JWT; **zone administrator** only. Accepts a **PENDING** guest pass, "
        "setting its status to **ACCEPTED**. Once accepted, the pass is active for auto-approval "
        "when a guest arrives at **`POST /api/access/permission`** with the matching **`event_id`**.\n\n"
        "**Side effects:** a persisted **`ZoneMessageEvent`** **`PERMISSION`** row (`metadata.flow` = **`guest_pass_lifecycle`**, "
        "**`body.code`** = **`GUEST_PASS_ACCEPTED`**) merged into **`GET /messages`** for eligible owners, plus a "
        "**`PERMISSION_MESSAGE`** WebSocket to **zone staff**."
    ),
    response_description="Updated guest pass with ACCEPTED status.",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Guest pass has expired (**EXPIRED**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not an administrator for this zone (**FORBIDDEN**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Guest pass not found (**NOT_FOUND**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_409_CONFLICT: {
            "description": "Guest pass is not in PENDING status (**INVALID_STATE**).",
            "model": GuestAccessHttpError,
        },
    },
)
async def accept_guest_pass(
    pass_id: str = Path(..., min_length=1, max_length=36, description="UUID of the guest pass to accept."),
    payload: GuestPassAdminRequest | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error_code": "NOT_FOUND", "message": "Owner not found"})

    result = guest_pass_service.accept_guest_pass(db, owner=owner, pass_id=pass_id)
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )

    row = result["row"]
    db.commit()
    db.refresh(row)

    ws_payload = guest_pass_service.build_guest_pass_ws_payload(
        db, guest_pass=row, code="GUEST_PASS_ACCEPTED", zone_id=row.zone_id,
    )
    member_ids = ws_payload["data"]["delivered_owner_ids"]
    if member_ids:
        await ws_manager.broadcast_to_users(member_ids, "PERMISSION_MESSAGE", ws_payload["data"])

    return GuestPassDecisionEnvelope(
        data=GuestPassDecisionData(
            id=row.id, status="ACCEPTED", reviewed_by=row.reviewed_by, updated_at=row.updated_at,
        )
    )


@router.post(
    "/guest-passes/{pass_id}/reject",
    response_model=GuestPassDecisionEnvelope,
    status_code=status.HTTP_200_OK,
    operation_id="access_reject_guest_pass",
    summary="Reject a guest pass (admin only)",
    description=(
        "**Bearer** JWT; **zone administrator** only. Rejects a **PENDING** guest pass, "
        "setting its status to **REJECTED**. A rejected pass cannot be used for auto-approval.\n\n"
        "**Side effects:** a persisted **`ZoneMessageEvent`** **`PERMISSION`** row (`metadata.flow` = **`guest_pass_lifecycle`**, "
        "**`body.code`** = **`GUEST_PASS_REJECTED`**) merged into **`GET /messages`** for eligible owners, plus a "
        "**`PERMISSION_MESSAGE`** WebSocket to **zone staff**."
    ),
    response_description="Updated guest pass with REJECTED status.",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Guest pass has expired (**EXPIRED**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not an administrator for this zone (**FORBIDDEN**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Guest pass not found (**NOT_FOUND**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_409_CONFLICT: {
            "description": "Guest pass is not in PENDING status (**INVALID_STATE**).",
            "model": GuestAccessHttpError,
        },
    },
)
async def reject_guest_pass(
    pass_id: str = Path(..., min_length=1, max_length=36, description="UUID of the guest pass to reject."),
    payload: GuestPassAdminRequest | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error_code": "NOT_FOUND", "message": "Owner not found"})

    result = guest_pass_service.reject_guest_pass(db, owner=owner, pass_id=pass_id)
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )

    row = result["row"]
    db.commit()
    db.refresh(row)

    ws_payload = guest_pass_service.build_guest_pass_ws_payload(
        db, guest_pass=row, code="GUEST_PASS_REJECTED", zone_id=row.zone_id,
    )
    member_ids = ws_payload["data"]["delivered_owner_ids"]
    if member_ids:
        await ws_manager.broadcast_to_users(member_ids, "PERMISSION_MESSAGE", ws_payload["data"])

    return GuestPassDecisionEnvelope(
        data=GuestPassDecisionData(
            id=row.id, status="REJECTED", reviewed_by=row.reviewed_by, updated_at=row.updated_at,
        )
    )


@router.post(
    "/guest-passes/{pass_id}/revoke",
    response_model=GuestPassDecisionEnvelope,
    status_code=status.HTTP_200_OK,
    operation_id="access_revoke_guest_pass",
    summary="Revoke an accepted guest pass (admin only)",
    description=(
        "**Bearer** JWT; **zone administrator** only. Revokes an already-**ACCEPTED** pass "
        "before it expires, setting status to **REVOKED**.\n\n"
        "If the pass has already been consumed (`used_by_guest_id` is set), the revocation "
        "also invalidates the guest's **guest_access** Bearer: **`/api/guest/*`** returns **401** with "
        "**`error_code`** **`GUEST_ACCESS_INVALIDATED`** until the client obtains a new session.\n\n"
        "**Side effects:** a persisted **`ZoneMessageEvent`** **`PERMISSION`** row (`metadata.flow` = **`guest_pass_lifecycle`**, "
        "**`body.code`** = **`GUEST_PASS_REVOKED`**, **`body.revoked_by`** = revoking admin **`owners.id`**) merged into "
        "**`GET /messages`** for eligible owners, plus a **`PERMISSION_MESSAGE`** WebSocket to **zone staff**. "
        "The stored **`message`** text includes that this Event ID will no longer auto-approve guests."
    ),
    response_description="Updated guest pass with REVOKED status.",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "Pass **REVOKED**. If **`used_by_guest_id`** was set, the matching **`guest_access_sessions`** row "
                "receives **`access_revoked_at`** so **`/api/guest/*`** returns **401** with **`GuestApiHttpError`** "
                "**`GUEST_ACCESS_INVALIDATED`**."
            ),
            "model": GuestPassDecisionEnvelope,
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "Guest pass has expired (**EXPIRED**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not an administrator for this zone (**FORBIDDEN**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Guest pass not found (**NOT_FOUND**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_409_CONFLICT: {
            "description": "Guest pass is not in ACCEPTED status (**INVALID_STATE**).",
            "model": GuestAccessHttpError,
        },
    },
)
async def revoke_guest_pass(
    pass_id: str = Path(..., min_length=1, max_length=36, description="UUID of the guest pass to revoke."),
    payload: GuestPassAdminRequest | None = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error_code": "NOT_FOUND", "message": "Owner not found"})

    result = guest_pass_service.revoke_guest_pass(db, owner=owner, pass_id=pass_id)
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )

    row = result["row"]
    db.commit()
    db.refresh(row)

    ws_payload = guest_pass_service.build_guest_pass_ws_payload(
        db, guest_pass=row, code="GUEST_PASS_REVOKED", zone_id=row.zone_id, acting_owner_id=owner.id,
    )
    member_ids = ws_payload["data"]["delivered_owner_ids"]
    if member_ids:
        await ws_manager.broadcast_to_users(member_ids, "PERMISSION_MESSAGE", ws_payload["data"])

    return GuestPassDecisionEnvelope(
        data=GuestPassDecisionData(
            id=row.id, status="REVOKED", reviewed_by=row.reviewed_by, updated_at=row.updated_at,
        )
    )


@router.get("/qr-tokens/primary", response_model=PrimaryGuestQrTokenEnvelope, status_code=status.HTTP_200_OK)
async def get_or_create_primary_guest_qr_token(
    zone_id: str = Query(..., min_length=1, max_length=100),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error_code": "NOT_FOUND", "message": "Owner not found"})
    result = guest_access_qr_token_service.get_or_create_primary_guest_qr_token(db, owner, zone_id=zone_id)
    if result.get("error"):
        raise HTTPException(status_code=result["http_status"], detail={"error_code": result["error"], "message": result["message"]})
    row = result["row"]
    db.commit()
    db.refresh(row)
    return PrimaryGuestQrTokenEnvelope(data=_primary_token_contract_payload(row))


@router.post("/qr-tokens/primary/rotate", response_model=PrimaryGuestQrTokenEnvelope, status_code=status.HTTP_200_OK)
async def rotate_primary_guest_qr_token(
    payload: PrimaryGuestQrRotateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error_code": "NOT_FOUND", "message": "Owner not found"})
    result = guest_access_qr_token_service.rotate_primary_guest_qr_token(
        db,
        owner,
        zone_id=payload.zone_id,
        reason=payload.reason,
    )
    if result.get("error"):
        raise HTTPException(status_code=result["http_status"], detail={"error_code": result["error"], "message": result["message"]})
    row = result["row"]
    db.commit()
    db.refresh(row)
    return PrimaryGuestQrTokenEnvelope(data=_primary_token_contract_payload(row))


@router.post(
    "/permission",
    response_model=AccessPermissionResponseEnvelope,
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
    summary=_PERM_SUMMARY,
    description=_PERM_DESCRIPTION.strip(),
    response_description=(
        "Guest-facing outcome, **`guest_id`** for polling, and **`zone_id`** for **`GET …/session/{guest_id}` "
        "(when **`zid`** was not already in the invite URL)."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Unknown zone (**INVALID_ZONE**) or unknown guest QR token (**INVALID_GUEST_TOKEN**).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": (
                "Validation failure, **NO_ZONE_ADMIN**, **TOKEN_ZONE_MISMATCH**, **EVENT_MISMATCH**, "
                "or related errors; see `error_code` in body."
            ),
            "model": GuestAccessHttpError,
        },
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "Guest QR token revoked, expired, or depleted (**TOKEN_REVOKED**, **TOKEN_EXPIRED**, **TOKEN_DEPLETED**)."
            ),
            "model": GuestAccessHttpError,
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "Too many anonymous arrivals from this client (**RATE_LIMITED**).",
            "model": GuestAccessHttpError,
        },
    },
)
async def guest_permission(request: Request, payload: GuestArrivalRequest, db: Session = Depends(get_db)):
    ip_key = _client_ip(request)
    if not allow_request(
        f"guest_perm:{ip_key}",
        max_events=settings.GUEST_ACCESS_PERMISSION_MAX_PER_MINUTE,
        window_seconds=60.0,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error_code": "RATE_LIMITED",
                "message": "Too many arrival attempts from this network. Please wait and try again.",
            },
        )

    qr_row = None
    raw_gt = (payload.guest_qr_token or "").strip()
    payload_zone = (payload.zone_id or "").strip() or None
    effective_zone_id = payload_zone
    effective_event_id = payload.event_id

    if raw_gt:
        qr_row = guest_access_qr_token_service.lock_guest_qr_token_row(db, raw_gt)
        verr = guest_access_qr_token_service.validate_locked_guest_qr_token(qr_row)
        if verr:
            logger.info(
                "guest_access_permission guest_qr_token outcome=error error_code=%s",
                verr["error"],
            )
            raise HTTPException(
                status_code=verr["http_status"],
                detail={"error_code": verr["error"], "message": verr["message"]},
            )

        tz_zone = qr_row.zone_id
        if payload_zone and payload_zone != tz_zone:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "TOKEN_ZONE_MISMATCH",
                    "message": "zone_id does not match the guest QR token.",
                },
            )
        effective_zone_id = tz_zone

        merged_ev, eerr = guest_access_qr_token_service.merge_event_id_for_arrival(
            token_event_id=qr_row.event_id,
            payload_event_id=payload.event_id,
        )
        if eerr:
            raise HTTPException(
                status_code=eerr["http_status"],
                detail={"error_code": eerr["error"], "message": eerr["message"]},
            )
        effective_event_id = merged_ev

    elif not effective_zone_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "MISSING_ZONE",
                "message": "zone_id is required when guest_qr_token is omitted.",
            },
        )

    lat = payload.location.lat if payload.location else None
    lng = payload.location.lng if payload.location else None

    guest_name = _sanitize_guest_name_or_422(payload.guest_name)

    result = guest_access_service.process_guest_arrival(
        db,
        zone_id=effective_zone_id,
        guest_name=guest_name,
        event_id=effective_event_id,
        device_id=payload.device_id,
        latitude=lat,
        longitude=lng,
        qr_token_db_id=qr_row.id if qr_row else None,
    )
    if result.get("error"):
        logger.info(
            "guest_access_permission zone_id=%s outcome=error error_code=%s",
            effective_zone_id,
            result["error"],
        )
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )

    gr = result["guest_response"]
    logger.info(
        "guest_access_permission zone_id=%s outcome=%s guest_id=%s",
        effective_zone_id,
        gr["status"],
        gr["guest_id"],
    )

    if qr_row:
        guest_access_qr_token_service.apply_successful_arrival_use(db, qr_row)

    db.commit()

    for user_ids, event_payload in result.get("ws_guest_is_here") or []:
        await ws_manager.broadcast_to_users(user_ids, "guest_is_here", event_payload)
    for user_ids, event_payload in result.get("ws_unexpected_guest") or []:
        await ws_manager.broadcast_to_users(user_ids, "unexpected_guest", event_payload)

    return AccessPermissionResponseEnvelope(data=AccessPermissionResponseData.model_validate(gr))


@router.get(
    "/qr-link",
    response_model=GuestAccessQrLinkResponse,
    status_code=status.HTTP_200_OK,
    summary="Canonical guest-access deep link",
    description=(
        "Requires **Bearer** JWT. Caller must be a **zone administrator** with **owner.zone_id** equal "
        "to **zone_id**. Returns the stable **zone-static** SPA path **`/access?zid=`** (optional **`eid=`**), "
        "and an absolute **url** when **GUEST_ACCESS_APP_BASE_URL** is configured. "
        "For opaque **stored guest tokens** (**`gt`** + **`zid`**), use **`POST /api/access/qr-tokens`** or **`GET …/qr-tokens/{id}/link`**. "
        "This is **not** the member-invite flow (`POST /utils/qr/generate`)."
    ),
    response_description="URL for QR encoding (no PII in query string).",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Wrong zone or not an administrator.",
            "model": GuestAccessHttpError,
        },
        status.HTTP_404_NOT_FOUND: {"description": "Authenticated owner not found."},
    },
)
async def guest_access_qr_link(
    zone_id: str = Query(..., min_length=1, max_length=100, description="Hex zone id encoded as `zid`."),
    event_id: str | None = Query(
        default=None,
        max_length=100,
        description="Optional; included as `eid` so guests are pre-associated with an event id.",
    ),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ = _require_guest_qr_administrator(db, current_user, zone_id)
    zid = zone_id.strip()
    path = guest_access_qr.guest_access_path_with_query(zid, event_id)
    absolute = guest_access_qr.guest_access_absolute_url(zid, event_id)
    return GuestAccessQrLinkResponse(url=absolute, zone_id=zid, path_with_query=path)


@router.get(
    "/qr.png",
    summary="PNG QR code for guest-access URL",
    description=(
        "**Zone-static** PNG ( **`/access?zid=`** ), same authorization and URL shape as **GET /api/access/qr-link**. "
        "Requires **GUEST_ACCESS_APP_BASE_URL** (or legacy **PUBLIC_WEB_APP_URL**) so the encoded URL is absolute. "
        "Stored-token PNGs (**`gt`**) use **GET `/api/access/qr-tokens/{qr_token_id}/qr.png`**."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {"description": "Wrong zone or not an administrator.", "model": GuestAccessHttpError},
        status.HTTP_404_NOT_FOUND: {"description": "Authenticated owner not found."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Web app base URL not configured (**GUEST_LINK_BASE_UNCONFIGURED**).",
            "model": GuestAccessHttpError,
        },
    },
    response_class=Response,
)
async def guest_access_qr_png(
    zone_id: str = Query(..., min_length=1, max_length=100),
    event_id: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _ = _require_guest_qr_administrator(db, current_user, zone_id)
    zid = zone_id.strip()
    url = guest_access_qr.guest_access_absolute_url(zid, event_id)
    if not url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "GUEST_LINK_BASE_UNCONFIGURED",
                "message": "Set GUEST_ACCESS_APP_BASE_URL so the API can build an absolute guest URL for the QR image.",
            },
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "GUEST_LINK_BASE_INVALID",
                "message": "GUEST_ACCESS_APP_BASE_URL must start with http:// or https://.",
            },
        )

    png = guest_access_qr.qr_png_bytes_for_url(url)
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=300",
        },
    )


@router.post(
    "/qr-tokens",
    response_model=GuestQrTokenCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create stored guest QR token",
    description=(
        "**Bearer** JWT; **administrator** for **zone_id**. Mints an opaque **guest_qr_token** used in "
        "`POST /api/access/permission` and SPA **`/access?gt=&zid=`**. Default TTL **168h** if neither "
        "**expires_at** nor **expires_in_hours** is sent."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {"model": GuestAccessHttpError},
        status.HTTP_404_NOT_FOUND: {"description": "Owner not found.", "model": GuestAccessHttpError},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": GuestAccessHttpError},
    },
)
async def create_guest_access_qr_token(
    payload: GuestQrTokenCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Owner not found"},
        )
    result = guest_access_qr_token_service.create_guest_qr_token(
        db,
        owner,
        zone_id=payload.zone_id,
        expires_at=payload.expires_at,
        expires_in_hours=payload.expires_in_hours,
        event_id=payload.event_id,
        label=payload.label,
        max_uses=payload.max_uses,
        is_primary=bool(payload.is_primary),
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )
    row = result["row"]
    db.commit()
    db.refresh(row)
    path = guest_access_qr.guest_access_path_with_guest_token(
        row.token,
        zone_id=row.zone_id,
        event_id=row.event_id,
    )
    url = guest_access_qr.guest_access_absolute_url_with_guest_token(
        row.token,
        zone_id=row.zone_id,
        event_id=row.event_id,
    )
    body = {
        **guest_access_qr_token_service.serialize_guest_qr_token_public(row),
        "token": row.token,
        "url": url,
        "path_with_query": path,
    }
    return GuestQrTokenCreatedResponse.model_validate(body)


@router.get(
    "/qr-tokens",
    response_model=list[GuestQrTokenListItem],
    summary="List stored guest QR tokens",
    description="**Administrator** JWT; **zone_id** query must match caller **owner.zone_id**.",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {"model": GuestAccessHttpError},
        status.HTTP_404_NOT_FOUND: {"model": GuestAccessHttpError},
    },
)
async def list_guest_access_qr_tokens(
    zone_id: str = Query(..., min_length=1, max_length=100),
    include_revoked: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Owner not found"},
        )
    result = guest_access_qr_token_service.list_guest_qr_tokens(
        db,
        owner,
        zone_id=zone_id.strip(),
        limit=limit,
        include_revoked=include_revoked,
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )
    return [
        GuestQrTokenListItem.model_validate(guest_access_qr_token_service.serialize_guest_qr_token_public(r))
        for r in result["rows"]
    ]


@router.post(
    "/qr-tokens/{qr_token_id}/revoke",
    response_model=GuestQrTokenListItem,
    summary="Revoke a stored guest QR token",
    description=(
        "Token stops accepting new arrivals immediately. Sessions already linked via **`guest_access_sessions.qr_token_id`** "
        "lose **`/api/guest/*`** access on the next request (**`401`** **`GUEST_ACCESS_INVALIDATED`**) until the guest re-authenticates."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {"model": GuestAccessHttpError},
        status.HTTP_404_NOT_FOUND: {"model": GuestAccessHttpError},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": GuestAccessHttpError},
    },
)
async def revoke_guest_access_qr_token(
    qr_token_id: int,
    zone_id: str = Query(..., min_length=1, max_length=100),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Owner not found"},
        )
    result = guest_access_qr_token_service.revoke_guest_qr_token(
        db,
        owner,
        zone_id=zone_id.strip(),
        token_row_id=qr_token_id,
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )
    row = result["row"]
    db.commit()
    db.refresh(row)
    return GuestQrTokenListItem.model_validate(guest_access_qr_token_service.serialize_guest_qr_token_public(row))


@router.get(
    "/qr-tokens/{qr_token_id}/link",
    response_model=GuestQrTokenLinkBundle,
    summary="Resolve URL for stored guest QR token",
    description=(
        "Returns **`path_with_query`** and absolute **`url`** when **GUEST_ACCESS_APP_BASE_URL** is set — "
        "same shape as **`POST /api/access/qr-tokens`**: **`/access?gt=…&zid=…`** (optional **`eid`**)."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {"model": GuestAccessHttpError},
        status.HTTP_404_NOT_FOUND: {"model": GuestAccessHttpError},
    },
)
async def guest_access_qr_token_link(
    qr_token_id: int,
    zone_id: str = Query(..., min_length=1, max_length=100),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Owner not found"},
        )
    result = guest_access_qr_token_service.get_guest_qr_token_row_admin(
        db,
        owner,
        zone_id=zone_id.strip(),
        token_row_id=qr_token_id,
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )
    row = result["row"]
    path = guest_access_qr.guest_access_path_with_guest_token(
        row.token,
        zone_id=row.zone_id,
        event_id=row.event_id,
    )
    url = guest_access_qr.guest_access_absolute_url_with_guest_token(
        row.token,
        zone_id=row.zone_id,
        event_id=row.event_id,
    )
    return GuestQrTokenLinkBundle(id=row.id, url=url, path_with_query=path)


@router.get(
    "/qr-tokens/{qr_token_id}/qr.png",
    summary="PNG QR for stored guest token URL",
    description=(
        "Encodes the same absolute URL as **`GET /api/access/qr-tokens/{id}/link`** "
        "(**`gt` + `zid`**, optional **`eid`**), not **`GET /api/access/qr.png`** (zone-static **`zid`** only)."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {"model": GuestAccessHttpError},
        status.HTTP_404_NOT_FOUND: {"model": GuestAccessHttpError},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": GuestAccessHttpError},
    },
    response_class=Response,
)
async def guest_access_qr_token_png(
    qr_token_id: int,
    zone_id: str = Query(..., min_length=1, max_length=100),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Owner not found"},
        )
    result = guest_access_qr_token_service.get_guest_qr_token_row_admin(
        db,
        owner,
        zone_id=zone_id.strip(),
        token_row_id=qr_token_id,
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )
    row = result["row"]
    url = guest_access_qr.guest_access_absolute_url_with_guest_token(
        row.token,
        zone_id=row.zone_id,
        event_id=row.event_id,
    )
    if not url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "GUEST_LINK_BASE_UNCONFIGURED",
                "message": "Set GUEST_ACCESS_APP_BASE_URL so the API can build an absolute guest URL for the QR image.",
            },
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "GUEST_LINK_BASE_INVALID",
                "message": "GUEST_ACCESS_APP_BASE_URL must start with http:// or https://.",
            },
        )
    png = guest_access_qr.qr_png_bytes_for_url(url)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get(
    "/session/{guest_id}",
    response_model=AccessSessionPollEnvelope,
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
    summary="Poll guest session status",
    description=(
        "Public poll for guest clients without a WebSocket. Provide **guest_id** from "
        "`POST /api/access/permission`. "
        "**`zone_id`** should match arrival (invite **`zid`**, **`gt`** QR `zid`, or **`zone_id`** in the permission response); "
        "when omitted the server resolves **guest_id** alone (opaque UUID).\n\n"
        "Each response includes tri-state **`status`** only: **PENDING** | **APPROVED** | **REJECTED**. "
        "Unexpected + pending maps to **PENDING**; expected or approved maps to **APPROVED**. "
        "**REJECTED** includes admin deny (**`resolution`** **`rejected`**), session revoke (**`access_revoked_at`**), "
        "and equivalent guest-facing states after **`POST /api/access/reject`** on expected arrivals.\n\n"
        "When **status** is **APPROVED** (expected guest, guest pass, schedule match, or admin-approved unexpected), "
        "the body includes a valid unused **`exchange_code`** and **`exchange_expires_at`** until the guest completes "
        "**`POST /api/access/guest-session`** (single-use; TTL from **`GUEST_ACCESS_EXCHANGE_TTL_MINUTES`**). "
        "Those fields are omitted after a successful exchange (**`exchange_consumed`**) or if the session is not cleared.\n\n"
        "**`message`:** For **PENDING** unexpected guests and for **APPROVED** while the guest is still in the initial "
        "expected or guest-pass path, text is the **arrival snapshot** (immutable for that session after **`POST /api/access/permission`**); "
        "see **`GET|PATCH|PUT /api/access/zones/{zone_id}/guest-arrival-messages`** in OpenAPI."
    ),
    response_description="Guest-visible status; one-time exchange when APPROVED and JWT exchange is still pending.",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "No matching guest session (unknown guest_id or wrong zone_id).",
            "model": GuestAccessHttpError,
        },
    },
)
async def guest_session_status(
    guest_id: str = Path(
        ...,
        min_length=1,
        max_length=36,
        description="Opaque id from **POST /api/access/permission** (UUID).",
    ),
    zone_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=100,
        description=(
            "Arrival zone. Omit only when resolving by guest_id alone "
            "(e.g. bookmarked **`/access?gt=`** without **`zid`**)."
        ),
    ),
    db: Session = Depends(get_db),
):
    q = db.query(GuestAccessSession).filter(GuestAccessSession.guest_id == guest_id.strip())
    z = (zone_id or "").strip()
    if z:
        q = q.filter(GuestAccessSession.zone_id == z)
    row = q.first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Unknown guest session."},
        )
    if guest_access_service.ensure_active_guest_exchange_for_poll(db, row):
        db.commit()
    view = guest_access_service.guest_session_public_view(db, row)
    if view["status"] == "UNEXPECTED":
        mapped_status = "PENDING"
    elif view["status"] == "EXPECTED":
        mapped_status = "APPROVED"
    else:
        mapped_status = view["status"]
    data = AccessSessionPollData(
        status=mapped_status,
        message=view.get("message"),
        exchange_code=view.get("exchange_code"),
        exchange_expires_at=view.get("exchange_expires_at"),
    )
    return AccessSessionPollEnvelope(data=data)


def _guest_arrival_messages_envelope_from_db(db: Session, zone_id: str) -> GuestArrivalMessagesEnvelope:
    payload = guest_arrival_zone_messages_service.guest_arrival_messages_admin_api_dict(db, zone_id)
    data = GuestArrivalMessagesData(
        zone_id=payload["zone_id"],
        expected_arrival_message=payload["expected_arrival_message"],
        unexpected_arrival_message=payload["unexpected_arrival_message"],
        guest_pass_verified_message=payload["guest_pass_verified_message"],
        defaults=GuestArrivalMessagesDefaults.model_validate(payload["defaults"]),
    )
    return GuestArrivalMessagesEnvelope(status="success", data=data)


@router.get(
    "/zones/{zone_id}/guest-arrival-messages",
    response_model=GuestArrivalMessagesEnvelope,
    status_code=status.HTTP_200_OK,
    summary="Read guest arrival message overrides for a zone",
    description=(
        "**Bearer** member JWT. Caller must be a **zone administrator** with **`owners.zone_id`** equal to **zone_id** "
        "and satisfy **`can_manage_zone_guest_requests`** (same rules as **`GET /api/access/guest-requests`**).\n\n"
        "Returns nullable per-field overrides plus **`defaults`** (built-in English strings). **JSON null** on a field "
        "means that slot uses the default for **new** arrivals only; **`GET /api/access/session/{guest_id}`** may still "
        "show a **snapshot** taken at arrival (see that route)."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {"model": GuestAccessHttpError},
        status.HTTP_404_NOT_FOUND: {"description": "Authenticated owner not found."},
    },
)
async def get_guest_arrival_messages(
    zone_id: str = Path(..., min_length=1, max_length=100, description="Hex zone id (**zid**)."),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_zone_guest_arrival_messages_admin(db, current_user, zone_id)
    return _guest_arrival_messages_envelope_from_db(db, zone_id)


@router.patch(
    "/zones/{zone_id}/guest-arrival-messages",
    response_model=GuestArrivalMessagesEnvelope,
    status_code=status.HTTP_200_OK,
    summary="Update guest arrival message overrides (partial)",
    description=(
        "Same authorization as **GET**. Only keys present in the JSON body are updated; **JSON null** clears a stored "
        "override for that slot (revert to built-in default for **new** arrivals). Strings are trimmed; whitespace-only "
        f"or longer than **{guest_arrival_zone_messages_service.MAX_GUEST_ARRIVAL_MESSAGE_LEN}** characters is **422**."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {"model": GuestAccessHttpError},
        status.HTTP_404_NOT_FOUND: {"description": "Authenticated owner not found."},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Validation error (empty trimmed string or over max length)."},
    },
)
@router.put(
    "/zones/{zone_id}/guest-arrival-messages",
    response_model=GuestArrivalMessagesEnvelope,
    status_code=status.HTTP_200_OK,
    summary="Update guest arrival message overrides (partial, same as PATCH)",
    description="Alias of **PATCH** for clients that prefer **PUT** with a partial JSON body.",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {"model": GuestAccessHttpError},
        status.HTTP_404_NOT_FOUND: {"description": "Authenticated owner not found."},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Validation error (empty trimmed string or over max length)."},
    },
)
async def patch_put_guest_arrival_messages(
    body: GuestArrivalMessagesPatch,
    zone_id: str = Path(..., min_length=1, max_length=100, description="Hex zone id (**zid**)."),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = _require_zone_guest_arrival_messages_admin(db, current_user, zone_id)
    updates = body.model_dump(exclude_unset=True)
    if updates:
        guest_arrival_zone_messages_service.upsert_guest_arrival_zone_messages(
            db,
            zone_id=zone_id.strip(),
            column_updates=updates,
            acting_owner_id=owner.id,
        )
        db.commit()
    return _guest_arrival_messages_envelope_from_db(db, zone_id)


@router.post(
    "/guest-session",
    response_model=GuestSessionExchangeResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange approval code for guest access token",
    description=(
        "**No authentication.** Exchange the one-time **`exchange_code`** (from "
        "**`GET /api/access/session/{guest_id}`** when **status** is **APPROVED**) together with "
        "**`guest_id`** and **`zone_id`**. The code is **consumed** on success and cannot be reused.\n\n"
        "Returns a short-lived JWT: use **`Authorization: Bearer <access_token>`** only on **`/api/guest/*`**. "
        "JWT lifetime: **`expires_in`** seconds (from env **`GUEST_ACCESS_TOKEN_EXPIRE_MINUTES`**). "
        "The server may return **`401`** **`GUEST_ACCESS_INVALIDATED`** on guest routes **before** natural **`exp`** "
        "if an admin revokes access (see **`POST /api/access/reject`**, guest-pass **`…/revoke`**, QR token revoke).\n\n"
        "Optional **`device_id`**: if the arrival session stored **`device_id`** from **POST /api/access/permission** "
        "and the client sends a different non-empty value, the server responds **403** **`device_mismatch`**."
    ),
    response_description="Success envelope with guest JWT and profile summary.",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "`exchange_invalid`, `exchange_expired`, or malformed body.",
            "model": GuestApiHttpError,
        },
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "**`guest_not_approved`** — session not cleared for exchange, exchange already consumed with invalid retry, "
                "or **`access_revoked_at`** set (admin revoked expected session). "
                "**`zone_mismatch`** or **`device_mismatch`** as documented."
            ),
            "model": GuestApiHttpError,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Unknown **`guest_id`** session (`NOT_FOUND`).",
            "model": GuestApiHttpError,
        },
        status.HTTP_409_CONFLICT: {
            "description": "`exchange_consumed` — code already used.",
            "model": GuestApiHttpError,
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "`RATE_LIMITED` — too many attempts per client IP per minute.",
            "model": GuestApiHttpError,
        },
    },
)
async def guest_session_exchange(
    request: Request,
    payload: GuestSessionExchangeRequest,
    db: Session = Depends(get_db),
):
    ip_key = _client_ip(request)
    if not allow_request(
        f"guest_session:{ip_key}",
        max_events=settings.GUEST_ACCESS_GUEST_SESSION_MAX_PER_MINUTE,
        window_seconds=60.0,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error_code": "RATE_LIMITED",
                "message": "Too many guest-session attempts from this network. Please wait and try again.",
                "error": {
                    "message": "Too many guest-session attempts from this network. Please wait and try again.",
                },
            },
        )

    result = guest_access_service.consume_guest_exchange_and_issue_context(
        db,
        guest_id=payload.guest_id.strip(),
        zone_id=payload.zone_id.strip(),
        exchange_code=payload.exchange_code.strip(),
        device_id=payload.device_id,
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={
                "error_code": result["error"],
                "message": result["message"],
                "error": {"message": result["message"]},
            },
        )
    row = result["row"]
    token, expires_in, _ = create_guest_access_token(
        guest_id=row.guest_id,
        zone_ids=[row.zone_id],
    )
    db.commit()
    return GuestSessionExchangeResponse(
        status="success",
        data=GuestSessionExchangeData(
            access_token=token,
            token_type="Bearer",
            expires_in=expires_in,
            guest=GuestSessionGuestProfile(
                guest_id=row.guest_id,
                display_name=row.guest_name,
                zone_ids=[row.zone_id],
                allowed_message_types=["CHAT"],
            ),
        ),
    )


@router.get(
    "/guest-requests",
    response_model=GuestRequestListContractEnvelope,
    status_code=status.HTTP_200_OK,
    operation_id="access_list_guest_requests",
    summary="List guest access sessions (member JWT)",
    description=(
        "**Canonical dashboard URL:** **`GET /api/access/guest-requests`** — Hex Zone member SPA / "
        "**Hex-Zone-Client** use this to populate Guest requests and PERMISSION/CHAT recipient pickers.\n\n"
        "**Bearer** member JWT (same **`Authorization: Bearer`** stack as **`POST /messages`**, **`/zones`**, …). "
        "Query **`zone_id`** (required).\n\n"
        "**Authorization:** caller must be allowed to administer the zone — any active **`zones`** row "
        "for this **`zone_id`** whose **`owner_id`** is in the caller’s account visibility list "
        "(`zone_listing_owner_ids`), or the caller’s primary **`owners.zone_id`** matches and the zone exists, "
        "or (administrators only) a linked active member has that **`zone_id`**.\n\n"
        "**Response:** schema **`GuestRequestListEnvelope`** — **`{ \"status\": \"success\", \"data\": [ … ] }`**. "
        "**`data`** may be empty when no sessions match filters (not an error).\n\n"
        "Returns **`guest_access_sessions`** newest **`created_at`** first. Each **`guest_id`** matches "
        "**`POST /api/access/permission`**, **`GET /api/access/session/{guest_id}`**, and "
        "**`POST /messages`** when posting **CHAT** with **`guest_id`** + **`zone_id`** "
        "(persists **`ZoneMessageEvent`** for **`GET /api/guest/messages`**).\n\n"
        "**Legacy:** **`GET /message-feature/access/guest-requests`** returns the same rows as a **raw JSON array** "
        "(no envelope).\n\n"
        "**Query:** **`status`** filters **PENDING** / **APPROVED** / **REJECTED** "
        "(case-insensitive; **GRANTED** / **DENIED** accepted). **`pending_only=true`** restricts to unexpected + pending. "
        "**`limit`** (1–200, default 50) and **`skip`** paginate."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": (
                "Body matches schema **`GuestRequestListEnvelope`** (see **Schemas**). "
                "Each **`guest_id`** is the opaque session id for **`POST /messages`** with **`guest_id`** + **`zone_id`**."
            ),
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Missing or invalid bearer token (or non-numeric JWT `sub`).",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "Caller cannot list guest requests for this zone. "
                "Body follows the global error handler: **`status`**, **`message`**, **`error_code`**."
            ),
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Authenticated owner record not found (rare; **`detail`** may be unstructured).",
        },
    },
    response_description=(
        "**`GuestRequestListEnvelope`**: **`status`** is always **`success`**; **`data`** is "
        "**`GuestAccessSessionListItem[]`** (newest first)."
    ),
)
async def list_guest_requests_for_access_api(
    zone_id: str = Query(
        ...,
        min_length=1,
        max_length=100,
        description="Hex zone id (**zid**); required.",
    ),
    filter_status: str | None = Query(
        default=None,
        max_length=32,
        alias="status",
        description="Optional filter: **PENDING**, **APPROVED**, **REJECTED** (case-insensitive; GRANTED/DENIED accepted).",
    ),
    pending_only: bool = Query(
        False,
        description="If true, only unexpected sessions still **pending** (same as legacy list).",
    ),
    limit: int = Query(50, ge=1, le=200, description="Max rows (most recent first)."),
    skip: int = Query(0, ge=0, le=10_000, description="Offset for pagination."),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    viewer = owner_crud.get_owner(db, current_user["user_id"])
    if not viewer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Owner not found"},
        )
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
    data = []
    for r in rows:
        if r.access_revoked_at is not None:
            row_status = "REJECTED"
        else:
            row_status = "ARRIVED" if r.kind == "expected" else (
                "PENDING" if r.resolution == "pending" else str(r.resolution or "").upper()
            )
        data.append(
            GuestRequestListItemContract(
                id=str(r.id),
                guest_id=r.guest_id,
                zone_id=r.zone_id,
                guest_name=r.guest_name,
                status=row_status,
                expectation=r.kind,
                created_at=r.created_at,
                hid=r.device_id,
            )
        )
    return GuestRequestListContractEnvelope(status="success", data=data)


@router.get(
    "/guest-messages",
    response_model=MemberGuestAccessThreadMessagesEnvelope,
    status_code=status.HTTP_200_OK,
    summary="List guest access thread for member/admin (PERMISSION + CHAT)",
    description=(
        "**Bearer member JWT.** Returns **`ZoneMessageResponse[]`** (UUID **`id`**, **`guest_id`** populated when inferable) — "
        "the same **`ZoneMessageEvent`** history the guest sees on **`GET /api/guest/messages`** (**PERMISSION** + **CHAT**), "
        "including member→guest **CHAT** from **`POST /messages`** with **`guest_id`** + **`zone_id`**.\n\n"
        "**`zone_id`** and **`guest_id`** are required. Optional **`with_owner_id`** narrows to one staff peer (**`owners.id`**), "
        "same semantics as **`GET /api/guest/messages`** with **`with_owner_id`**, equivalent to **`GET /messages`** with **`guest_id`** + **`other_owner_id`** "
        "(peer filter; does **not** use the merged global inbox—it is the scoped Access thread).\n\n"
        "**Authorization** matches **`GET /api/access/guest-requests`**: zone administrator **`and`** **`can_manage_zone_guest_requests`**."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {"model": GuestAccessHttpError},
        status.HTTP_404_NOT_FOUND: {"description": "Owner or guest session not found."},
    },
)
async def list_guest_access_messages_for_member(
    zone_id: str = Query(..., min_length=1, max_length=100, description="Hex zone id (**zid**)."),
    guest_id: str = Query(..., min_length=1, max_length=36, description="Opaque id from **`POST /api/access/permission`**."),
    with_owner_id: int | None = Query(
        default=None,
        ge=1,
        description="Optional; restrict to DM with this **owners.id** (peer).",
    ),
    skip: int = Query(0, ge=0, le=10_000),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    viewer = owner_crud.get_owner(db, current_user["user_id"])
    if not viewer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Owner not found"},
        )
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
                "message": "You are not allowed to view guest messages for this zone.",
            },
        )
    row = guest_access_service.get_guest_access_session_by_guest_id(db, guest_id.strip())
    if not row or row.zone_id != zid:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "GUEST_NOT_FOUND", "message": "Guest session not found for this zone."},
        )
    events = guest_api_service.list_guest_access_thread_for_zone_member(
        db,
        zone_id=zid,
        guest_id=guest_id.strip(),
        peer_owner_id=with_owner_id,
        skip=skip,
        limit=limit,
        viewer_owner_id=viewer.id,
    )
    items = [guest_api_service.zone_message_event_to_member_zone_message_response(e, db=db) for e in events]
    return MemberGuestAccessThreadMessagesEnvelope(
        status="success",
        data=MemberGuestAccessThreadMessagesData(items=items),
    )


@router.post(
    "/approve",
    response_model=GuestAdminDecisionResponse,
    status_code=status.HTTP_200_OK,
    summary="Approve unexpected guest",
    description=(
        "Requires **Bearer** JWT. Caller must be an **administrator** with **owner.zone_id** equal to "
        "**payload.zone_id**. Only **`unexpected`** sessions in **`pending`** can be approved.\n\n"
        "On success, mints a one-time **`exchange_code`** (see **`GET /api/access/session/{guest_id}`**) "
        "for **`POST /api/access/guest-session`**.\n\n"
        "**Dashboard SPA alternative:** **`POST /message-feature/access/guest-requests/{requestId}/approve`** "
        "with request row **id** in the path (server infers **`zone_id`** from the persisted session; optional **`?zone_id=`** legacy)."
    ),
    response_description="Resolution copied from audit message; guest learns via polling.",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not a zone administrator (`FORBIDDEN`).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Owner not found or guest session not found (`NOT_FOUND`).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": (
                "Session not **`unexpected`**, already resolved, or **`access_revoked_at`** already set (`INVALID_STATE`)."
            ),
            "model": GuestAccessHttpError,
        },
    },
)
async def approve_guest(
    payload: GuestZoneActionRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")

    result = guest_access_service.approve_guest(
        db,
        acting_owner=owner,
        zone_id=payload.zone_id.strip(),
        guest_id=payload.guest_id.strip(),
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )
    db.commit()
    return GuestAdminDecisionResponse.model_validate(result["guest_response"])


@router.post(
    "/reject",
    response_model=GuestAdminDecisionResponse,
    status_code=status.HTTP_200_OK,
    summary="Reject or revoke guest access",
    description=(
        "Same authorization rules as **approve**. Denies a **pending** unexpected guest (**`resolution`** → **`rejected`**). "
        "Also revokes an **approved** unexpected guest after they may have exchanged a JWT, and revokes **expected** "
        "(schedule / auto-approved) sessions by setting **`access_revoked_at`** — in all cases the guest's "
        "**`/api/guest/*`** Bearer then returns **401** **`GUEST_ACCESS_INVALIDATED`**. Guest observes "
        "via **`GET /api/access/session/{guest_id}`**.\n\n"
        "**Dashboard SPA alternative:** **`POST /message-feature/access/guest-requests/{requestId}/reject`** "
        "(path request row **id**, inferred zone; optional **`?zone_id=`** legacy)."
    ),
    response_description="Resolution payload for admin client.",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or invalid bearer token."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not a zone administrator (`FORBIDDEN`).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Owner not found or guest session not found (`NOT_FOUND`).",
            "model": GuestAccessHttpError,
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Session **`unexpected`** already **`rejected`**, or **`access_revoked_at`** already set (`INVALID_STATE`).",
            "model": GuestAccessHttpError,
        },
    },
)
async def reject_guest(
    payload: GuestZoneActionRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": "Owner not found"},
        )

    result = guest_access_service.reject_guest(
        db,
        acting_owner=owner,
        zone_id=payload.zone_id.strip(),
        guest_id=payload.guest_id.strip(),
    )
    if result.get("error"):
        raise HTTPException(
            status_code=result["http_status"],
            detail={"error_code": result["error"], "message": result["message"]},
        )
    db.commit()
    return GuestAdminDecisionResponse.model_validate(result["guest_response"])
