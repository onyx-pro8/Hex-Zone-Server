"""Router for utility endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.schemas import (
    H3ConversionRequest,
    H3ConversionResponse,
    QRInviteExportRequest,
    QRInviteExportResponse,
    QRRegistrationCreate,
    QRRegistrationPreview,
    QRRegistrationResponse,
    QRRegistrationUse,
    OwnerResponse,
    QRJoinOwnerResponse,
)
from app.core.h3_utils import lat_lng_to_h3_cell
from app.core.security import get_current_user
from app.crud import qr_registration as qr_crud
from app.crud import owner as owner_crud
from app.models.owner import Owner, OwnerRole
from app.services.registration_code_service import (
    issue_registration_code_for_email_tier,
    mint_registration_code,
)
from app.services.device_entitlements import (
    account_type_supports_member_invite,
    admin_user_members_at_capacity,
    assert_admin_user_member_capacity,
)
from app.services.member_join_welcome_service import notify_members_of_new_join
from app.services.account_type_policy import (
    account_type_for_invited_member,
    is_system_administrator,
)
from app.services.system_admin_seed import (
    SYSTEM_ADMIN_EMAIL,
    SYSTEM_ADMIN_ZONE_ID,
    ensure_system_admin,
)
from app.services.guest_access_qr import guest_access_web_base
from app.services.member_invite_excel_service import (
    InviteExportRow,
    build_member_invite_xlsx_bytes,
    member_invite_join_url,
    store_invite_export,
    take_invite_export,
)

router = APIRouter(prefix="/utils", tags=["utilities"])


class SystemAdminEnsureResponse(BaseModel):
    email: str
    zone_id: str
    account_type: str = "private"
    message: str


class RegistrationCodeResponse(BaseModel):
    registration_code: str = Field(description="Single-use code for administrator registration")


class RegistrationCodeIssueRequest(BaseModel):
    email: EmailStr = Field(description="Administrator email used to derive the HMAC REG-CODE")
    pricing_tier: str = Field(
        description=(
            "Pricing tier: private | private_plus | exclusive | enhanced | enhanced_plus"
        ),
        validation_alias="pricingTier",
    )
    tier_level: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="Required for enhanced_plus (1–5 user-capacity levels)",
        validation_alias="tierLevel",
    )

    model_config = {"populate_by_name": True}


class SupportContactResponse(BaseModel):
    name: str
    email: str
    phone: str
    website: str


class EmailDeliveryResponse(BaseModel):
    sent: bool
    delivery: str
    reason: str | None = None


class RegistrationCodeIssueResponse(BaseModel):
    registration_code: str = Field(description="HMAC-derived REG-CODE (XXXXXX-XXXXXX)")
    api_key: str = Field(description="Pre-allocated API key bound to this issuance")
    pricing_tier: str
    tier_level: int | None = None
    pricing_tier_label: str
    expires_at: str = Field(description="UTC ISO-8601 expiration timestamp")
    email: str
    contact: SupportContactResponse
    email_delivery: EmailDeliveryResponse

    model_config = {"populate_by_name": True}


class EmailAvailableResponse(BaseModel):
    email: str
    available: bool
    message: str


@router.get(
    "/email-available",
    response_model=EmailAvailableResponse,
    summary="Check whether an email can be used for registration",
    description=(
        "Public endpoint (no Authorization). Returns whether the email is free for a new "
        "account. Used by the multi-step signup/invite onboarding flow before collecting "
        "the rest of the profile."
    ),
    responses={
        200: {
            "description": "Availability result for the supplied email.",
            "content": {
                "application/json": {
                    "example": {
                        "email": "alex@example.com",
                        "available": True,
                        "message": "Email is available.",
                    }
                }
            },
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Email is missing or not a valid address.",
        },
    },
)
async def check_email_available(
    email: EmailStr = Query(..., description="Email address to check"),
    db: Session = Depends(get_db),
):
    normalized = str(email).strip().lower()
    existing = owner_crud.get_owner_by_email(db, normalized)
    if existing:
        return EmailAvailableResponse(
            email=normalized,
            available=False,
            message="Email already registered",
        )
    return EmailAvailableResponse(
        email=normalized,
        available=True,
        message="Email is available.",
    )


@router.get(
    "/registration-code",
    response_model=RegistrationCodeResponse,
    summary="Issue registration code",
    description=(
        "Public endpoint (no Authorization). Returns a single-use registration code string "
        "for administrator self-registration. Send the same value as registrationCode on "
        "POST /register (contract) or registration_code on POST /owners/register. "
        "Codes expire after REGISTRATION_CODE_EXPIRE_HOURS (default 24). "
        "The tier code FREE is also accepted on those POST routes without calling this endpoint."
    ),
    responses={
        200: {
            "description": "Plain object with registration_code, or alternate keys per client parser.",
            "content": {
                "application/json": {
                    "example": {"registration_code": "url-safe-token-or-tier-FREE"}
                }
            },
        }
    },
    response_description="Generated single-use registration code object.",
)
async def issue_utils_registration_code(db: Session = Depends(get_db)):
    """Mint a DB-backed registration code (same semantics as GET /owners/registration-code)."""
    code = mint_registration_code(db)
    if code != "FREE":
        db.commit()
    return {"registration_code": code}


@router.post(
    "/registration-code/issue",
    response_model=RegistrationCodeIssueResponse,
    summary="Issue HMAC registration code by email and pricing tier",
    description=(
        "Public endpoint (no Authorization). Generates a deterministic HMAC REG-CODE from "
        "the administrator email and selected pricing tier, pre-allocates an API key, "
        "persists a single-use issuance row, and emails the REG-CODE + api-key + support "
        "contact details to the administrator. For **enhanced_plus**, include **tier_level** "
        "(1–5). Legacy GET /utils/registration-code remains available for mobile clients."
    ),
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Paid pricing tier — subscription upgrade required before code issuance.",
        },
        status.HTTP_409_CONFLICT: {"description": "Email already registered."},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Invalid pricing tier or tier_level."},
    },
)
async def issue_hmac_registration_code(
    body: RegistrationCodeIssueRequest,
    db: Session = Depends(get_db),
):
    result = issue_registration_code_for_email_tier(
        db,
        email=str(body.email),
        pricing_tier=body.pricing_tier,
        tier_level=body.tier_level,
    )
    db.commit()
    return result


@router.post(
    "/system-admin/ensure",
    response_model=SystemAdminEnsureResponse,
    summary="Ensure built-in system administrator exists",
    description=(
        "Idempotent bootstrap for the fixed Private-tier system administrator "
        f"({SYSTEM_ADMIN_EMAIL}). Safe to call after deploy when the account is missing."
    ),
)
async def ensure_system_admin_endpoint(db: Session = Depends(get_db)):
    owner = ensure_system_admin(db)
    return SystemAdminEnsureResponse(
        email=owner.email,
        zone_id=owner.zone_id,
        account_type=owner.account_type.value,
        message="System administrator is ready.",
    )


@router.post(
    "/h3/convert",
    response_model=H3ConversionResponse,
    summary="Convert coordinate to H3",
    description="Convert latitude/longitude to H3 cell for zone setup flows.",
    response_description="Converted coordinate plus computed H3 cell and effective resolution.",
)
async def convert_to_h3(
    request: H3ConversionRequest,
):
    """Convert latitude/longitude to H3 cell ID."""
    h3_cell_id = lat_lng_to_h3_cell(
        request.latitude,
        request.longitude,
        request.resolution,
    )
    
    from app.core.h3_utils import get_h3_resolution
    resolution = get_h3_resolution(h3_cell_id)
    
    return H3ConversionResponse(
        latitude=request.latitude,
        longitude=request.longitude,
        h3_cell_id=h3_cell_id,
        resolution=resolution,
    )


@router.post(
    "/qr/generate",
    response_model=QRRegistrationResponse,
    summary="Generate QR registration token",
    description=(
        "Generate invite token used by the QR registration flow. "
        "Not for door guest access — use **`GET /api/access/qr-link`** for canonical **`/access?zid=`** URLs. "
        "**System administrator (Private):** provisions a new **Individual (Exclusive)** user "
        "account for a new network (invitee chooses their own network ID on join). "
        "**Family (Private+) / Organization (Enhanced+):** invite a **user-role** member with "
        "the same account type onto the inviter's network. "
        "**Individual Pro (Enhanced):** invite one **Individual** user onto the primary zone. "
        "**Exclusive** (solo Individual) accounts cannot generate these invites. "
        "Each invite mints a unique Communal ID for the invitee (Individuals cannot "
        "generate one themselves). "
        "Send **`expires_in_hours`: 0** (or **null**) for a never-expiring "
        "**single-use** token. All invite tokens are single-use."
    ),
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "Caller is not an administrator of an invite-capable tier, or a "
                "non–system-admin has already reached invited-user capacity."
            ),
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Authenticated owner was not found.",
        },
    },
    response_description="Created QR registration token metadata.",
)
async def generate_qr_registration(
    qr_request: QRRegistrationCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate QR registration token for member invite or new-network provisioning."""
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )

    if owner.role.value != "administrator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can generate QR registration codes",
        )
    if not account_type_supports_member_invite(owner.account_type.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This account type does not support member invite QR codes "
                "(Enhanced accounts are solo administrator only)"
            ),
        )

    # System-admin tokens provision new Exclusive network admins (not members),
    # so invited-user capacity does not apply.
    if not is_system_administrator(owner):
        assert_admin_user_member_capacity(db, owner)

    qr = qr_crud.create_qr_registration(
        db,
        current_user["user_id"],
        qr_request.expires_in_hours,
    )
    db.commit()

    return QRRegistrationResponse.model_validate(qr)


@router.post(
    "/qr/export-xlsx",
    response_model=QRInviteExportResponse,
    summary="Export invite tokens as Excel with QR images",
    description=(
        "Build an `.xlsx` workbook for the given invite tokens (owned by the caller). "
        "Each row includes the invite's pre-issued Communal ID, token metadata, and an "
        "embedded QR code image for the join URL. Returns a short-lived **download_url** "
        "the mobile app can open. Restricted to the platform system administrator; "
        "network admins may generate a single invite QR but cannot export."
    ),
)
async def export_qr_invites_xlsx(
    body: QRInviteExportRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )
    if owner.role.value != "administrator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can export invite QR workbooks",
        )
    # Multi-QR Excel download is restricted to the platform system administrator.
    # Network (zone) admins may generate a single invite QR but cannot export/download.
    if not is_system_administrator(owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the system administrator can download invite QR workbooks",
        )

    web_base = (
        (body.join_base_url or "").strip()
        or guest_access_web_base()
        or str(request.base_url).rstrip("/")
    )

    rows: list[InviteExportRow] = []
    seen: set[str] = set()
    for raw in body.tokens:
        token = (raw or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        qr = qr_crud.get_qr_registration(db, token)
        if not qr or int(qr.owner_id) != int(owner.id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Invite token not found: {token[:12]}…",
            )
        expires = (
            qr.expires_at.isoformat()
            if getattr(qr, "expires_at", None) is not None
            else None
        )
        # Per-invite Communal ID minted for the future Individual member.
        invite_communal_id = qr_crud.ensure_qr_communal_id(db, qr)
        rows.append(
            InviteExportRow(
                index=len(rows) + 1,
                token=qr.token,
                url=member_invite_join_url(qr.token, web_base=web_base),
                expires_at=expires,
                communal_id=invite_communal_id,
            )
        )

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No valid invite tokens to export.",
        )

    db.commit()
    content = build_member_invite_xlsx_bytes(rows)
    file_name = f"member-invites-{len(rows)}.xlsx"
    export_id = store_invite_export(
        owner_id=int(owner.id),
        content=content,
        file_name=file_name,
    )
    download_url = str(request.url_for("download_qr_invite_export", export_id=export_id))
    return QRInviteExportResponse(
        download_url=download_url,
        file_name=file_name,
        expires_in_seconds=3600,
    )


@router.get(
    "/qr/exports/{export_id}",
    name="download_qr_invite_export",
    summary="Download exported invite Excel workbook",
    description=(
        "Public short-lived download for a workbook created by "
        "**POST /utils/qr/export-xlsx**. The export id is an unguessable secret."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Export missing or expired."},
    },
)
async def download_qr_invite_export(export_id: str):
    found = take_invite_export(export_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Export not found or expired.",
        )
    path, file_name = found
    return FileResponse(
        path=str(path),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        filename=file_name,
    )


def _load_valid_qr_and_inviter(db: Session, token: str):
    """Validate token lifecycle and return (qr, inviter_admin)."""
    qr = qr_crud.get_qr_registration(db, token)
    if not qr:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid or expired QR registration token",
        )

    if qr.used and not qr.is_reusable():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="QR registration token already used",
        )

    if qr.is_expired():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="QR registration token has expired",
        )

    owner = owner_crud.get_owner(db, qr.owner_id)
    if (
        not owner
        or owner.role.value != "administrator"
        or not account_type_supports_member_invite(owner.account_type.value)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid QR registration token",
        )
    return qr, owner


@router.get(
    "/qr/preview",
    response_model=QRRegistrationPreview,
    summary="Preview QR invite token",
    description=(
        "Public (no auth) preview of a QR invite. Clients use this to choose the "
        "join form: **member** (user-role member on inviter zone; Family/Org inherit "
        "inviter account type, Individual Pro invites are Individual) vs **new_network_admin** "
        "(Individual user account for a new network ID supplied on join)."
    ),
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "QR token already used (timed) or expired.",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "QR token is invalid for account join policy.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "QR token not found.",
        },
    },
)
async def preview_qr_registration(
    token: str,
    db: Session = Depends(get_db),
):
    """Return invite kind for the join UI."""
    _, owner = _load_valid_qr_and_inviter(db, token.strip())
    if is_system_administrator(owner):
        return QRRegistrationPreview(
            invite_kind="new_network_admin",
            account_type="exclusive",
            zone_id=None,
            members_at_capacity=False,
        )
    member_type = account_type_for_invited_member(owner)
    return QRRegistrationPreview(
        invite_kind="member",
        account_type=member_type.value,
        zone_id=owner.zone_id,
        members_at_capacity=admin_user_members_at_capacity(db, owner),
    )


@router.post(
    "/qr/join",
    response_model=QRJoinOwnerResponse,
    summary="Join account with QR token",
    description=(
        "Complete registration by consuming an invite token from the QR flow. "
        "**System administrator (Private) tokens:** create an **Individual** "
        "user account for a **new** network; require **`zone_id`** in the body. "
        "**Family / Organization admins:** create a **user-role** member with the same "
        "account type on the inviter's zone. "
        "**Individual Pro:** create an **Individual** user member on the inviter's zone "
        "(max one invited seat). "
        "All invite tokens (timed and never-expiring) are single-use."
    ),
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "QR token already used or expired.",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "QR token is invalid for account join policy, or the inviter "
                "account has no remaining member seats (invitee may sign up as "
                "an independent Individual instead)."
            ),
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "QR token not found.",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Email already registered, or network ID already in use.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Missing zone_id on a system-administrator invite.",
        },
    },
    response_description="Newly created owner account from QR flow.",
)
async def join_with_qr(
    qr_data: QRRegistrationUse,
    db: Session = Depends(get_db),
):
    """Redeem a QR invite as a member or as a new Individual network account."""
    qr, owner = _load_valid_qr_and_inviter(db, qr_data.token)

    existing = owner_crud.get_owner_by_email(db, qr_data.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    from app.schemas.schemas import OwnerCreate, AccountTypeEnum, OwnerRoleEnum

    if is_system_administrator(owner):
        new_zone_id = (qr_data.zone_id or "").strip()
        if not new_zone_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "zone_id is required when joining via a system administrator "
                    "invite (new Individual network)"
                ),
            )
        conflict = (
            db.query(Owner)
            .filter(
                Owner.zone_id == new_zone_id,
                Owner.active.is_(True),
            )
            .first()
        )
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Network ID already in use",
            )

        new_owner_data = OwnerCreate(
            email=qr_data.email,
            zone_id=new_zone_id,
            first_name=qr_data.first_name,
            last_name=qr_data.last_name,
            password=qr_data.password,
            account_type=AccountTypeEnum.EXCLUSIVE,
            role=OwnerRoleEnum.USER,
            account_owner_id=None,
            address=qr_data.address,
            phone=qr_data.phone,
        )
        new_owner = owner_crud.create_owner(
            db,
            new_owner_data,
            communal_id=qr_crud.ensure_qr_communal_id(db, qr),
        )

        if not qr.is_reusable():
            qr_crud.mark_qr_registration_used(db, qr.token)
        db.commit()

        return QRJoinOwnerResponse(
            **OwnerResponse.model_validate(new_owner).model_dump(),
            join_welcome_message=None,
        )

    # Network-admin member invite: inherit inviter zone; account type from policy.
    assert_admin_user_member_capacity(db, owner, invitee_facing=True)

    member_account_type = account_type_for_invited_member(owner)
    new_owner_data = OwnerCreate(
        email=qr_data.email,
        zone_id=owner.zone_id,
        first_name=qr_data.first_name,
        last_name=qr_data.last_name,
        password=qr_data.password,
        account_type=AccountTypeEnum(member_account_type.value),
        role=OwnerRoleEnum.USER,
        account_owner_id=owner.id,
        address=qr_data.address,
        phone=qr_data.phone,
    )

    new_owner = owner_crud.create_owner(
        db,
        new_owner_data,
        communal_id=qr_crud.ensure_qr_communal_id(db, qr),
    )

    if not qr.is_reusable():
        qr_crud.mark_qr_registration_used(db, qr.token)
    db.commit()

    welcome = await notify_members_of_new_join(db, new_owner)
    return QRJoinOwnerResponse(
        **OwnerResponse.model_validate(new_owner).model_dump(),
        join_welcome_message=welcome,
    )
