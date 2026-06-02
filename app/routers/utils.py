"""Router for utility endpoints."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.schemas import (
    H3ConversionRequest,
    H3ConversionResponse,
    QRRegistrationCreate,
    QRRegistrationResponse,
    QRRegistrationUse,
    OwnerResponse,
)
from app.core.h3_utils import lat_lng_to_h3_cell
from app.core.security import get_current_user
from app.crud import qr_registration as qr_crud
from app.crud import owner as owner_crud
from app.services.registration_code_service import (
    issue_registration_code_for_email_tier,
    mint_registration_code,
)
from app.services.device_entitlements import assert_admin_user_member_capacity

router = APIRouter(prefix="/utils", tags=["utilities"])


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


QR_INVITE_ALLOWED_ACCOUNT_TYPES = {"private", "exclusive"}


@router.post(
    "/qr/generate",
    response_model=QRRegistrationResponse,
    summary="Generate QR registration token",
    description=(
        "Generate invite token used by **account/member join** QR flow only. "
        "Not for door guest access — use **`GET /api/access/qr-link`** for canonical **`/access?zid=`** URLs. "
        "Available to administrators of **Private** (multi-user) and **Exclusive** "
        "(admin + 1 invited user) account tiers."
    ),
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "Caller is not an administrator of an invite-capable tier, or the "
                "administrator has already reached their invited-user capacity."
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
    """Generate QR registration token for inviting a user member."""
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
    if owner.account_type.value not in QR_INVITE_ALLOWED_ACCOUNT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only Private and Exclusive accounts can generate QR registration codes"
            ),
        )

    # Reject up-front when the administrator has already filled their invited-user
    # capacity so they get a clean error instead of a token that cannot be redeemed.
    assert_admin_user_member_capacity(db, owner)

    qr = qr_crud.create_qr_registration(
        db,
        current_user["user_id"],
        qr_request.expires_in_hours,
    )
    db.commit()

    return QRRegistrationResponse.model_validate(qr)


@router.post(
    "/qr/join",
    response_model=OwnerResponse,
    summary="Join account with QR token",
    description="Complete registration by consuming invite token from QR flow.",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "QR token already used or expired.",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "QR token is invalid for account join policy.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "QR token not found.",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Email already registered.",
        },
    },
    response_description="Newly created owner account from QR flow.",
)
async def join_with_qr(
    qr_data: QRRegistrationUse,
    db: Session = Depends(get_db),
):
    """Join a Private account using QR registration token."""
    # Find QR registration by token
    qr = qr_crud.get_qr_registration(db, qr_data.token)
    if not qr:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid or expired QR registration token",
        )
    
    # Check if already used
    if qr.used:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="QR registration token already used",
        )
    
    # Check if expired
    if qr.is_expired():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="QR registration token has expired",
        )
    
    # Ensure this token belongs to an invite-capable administrator
    owner = owner_crud.get_owner(db, qr.owner_id)
    if (
        not owner
        or owner.role.value != "administrator"
        or owner.account_type.value not in QR_INVITE_ALLOWED_ACCOUNT_TYPES
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid QR registration token",
        )

    # Final capacity gate: e.g. Exclusive admin already has their 1 invited user.
    assert_admin_user_member_capacity(db, owner)

    # Check if email already exists
    existing = owner_crud.get_owner_by_email(db, qr_data.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    # Create new owner inheriting the inviter's account type so an Exclusive
    # invite stays Exclusive (admin + 1 user) and a Private invite stays Private.
    from app.schemas.schemas import OwnerCreate, AccountTypeEnum, OwnerRoleEnum
    new_owner_data = OwnerCreate(
        email=qr_data.email,
        # Enforce inviter zone ownership for all QR-based joins.
        zone_id=owner.zone_id,
        first_name=qr_data.first_name,
        last_name=qr_data.last_name,
        password=qr_data.password,
        account_type=AccountTypeEnum(owner.account_type.value),
        role=OwnerRoleEnum.USER,
        account_owner_id=owner.id,
        address=qr_data.address,
        phone=qr_data.phone,
    )

    new_owner = owner_crud.create_owner(db, new_owner_data)

    # Mark QR as used
    qr_crud.mark_qr_registration_used(db, qr.token)
    db.commit()

    return OwnerResponse.model_validate(new_owner)
