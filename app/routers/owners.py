"""Router for Owner/User endpoints."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.schemas import (
    OwnerCreate,
    OwnerResponse,
    OwnerUpdate,
    OwnerDetailResponse,
    OwnerListResponse,
    LoginRequest,
    TokenResponse,
    OwnerRoleEnum,
)
from app.crud import owner as owner_crud
from app.core.security import get_current_user, verify_password, create_access_token
from app.services.access_policy import resolve_account_owner_id, visible_owner_ids, messaging_visible_owner_ids
from app.services.registration_code_service import (
    mint_registration_code,
    require_and_consume_admin_registration_code,
)
from app.services.account_type_policy import (
    assert_account_type_allowed_for_public_registration,
    assert_owner_may_edit_network_id,
)
from app.services.member_join_welcome_service import notify_members_of_new_join
router = APIRouter(prefix="/owners", tags=["owners"])


def _normalize_owner_name(owner):
    if not owner.last_name:
        owner.last_name = owner.first_name or "User"
    return owner


@router.post(
    "/register",
    response_model=OwnerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register account",
    description=(
        "Create an administrator/user account from setup wizard inputs. "
        "Supports all account tiers and links user registrations to an account owner. "
        "User registration must match administrator zone/account_type, and exclusive "
        "accounts cannot add user members. "
        "Administrators must include registration_code: echo GET /utils/registration-code "
        "(preferred) or GET /owners/registration-code, or use tier code FREE (stateless)."
    ),
    response_description="Registered account profile with API key",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Email already registered.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Registration validation failed (including invalid registration code rules).",
        },
    },
)
async def register_owner(
    owner: OwnerCreate,
    db: Session = Depends(get_db),
):
    """Register a new owner."""
    # Check if email already exists
    existing_owner = owner_crud.get_owner_by_email(db, owner.email)
    if existing_owner:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    assert_account_type_allowed_for_public_registration(owner.account_type.value)

    owner.account_owner_id = resolve_account_owner_id(
        db,
        role=owner.role.value,
        requested_account_owner_id=owner.account_owner_id,
        zone_id=owner.zone_id,
        account_type=owner.account_type.value,
    )

    preallocated_api_key: str | None = None
    if owner.role == OwnerRoleEnum.ADMINISTRATOR:
        preallocated_api_key = require_and_consume_admin_registration_code(
            db,
            owner.registration_code,
            registration_email=owner.email,
            account_type=owner.account_type.value,
        )

    db_owner = owner_crud.create_owner(db, owner, api_key=preallocated_api_key)
    db.commit()

    if owner.role == OwnerRoleEnum.USER:
        await notify_members_of_new_join(db, db_owner)

    return OwnerResponse.model_validate(_normalize_owner_name(db_owner))


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login",
    description="Authenticate with email and password and return bearer token.",
    response_description="JWT access token and owner id",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Invalid email or password.",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Account is inactive or expired.",
        },
    },
)
async def login(
    credentials: LoginRequest,
    db: Session = Depends(get_db),
):
    """Login with email and password."""
    owner = owner_crud.get_owner_by_email(db, credentials.email)
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    
    if not verify_password(credentials.password, owner.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    
    if not owner.active or owner.expired:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive or expired",
        )
    
    access_token = create_access_token(data={"sub": str(owner.id)})
    
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        owner_id=owner.id,
    )


@router.get(
    "/registration-code",
    summary="Issue registration code (owners alias)",
    description=(
        "Same as GET /utils/registration-code: public, unauthenticated, returns a single-use "
        "registration code for administrator signup. Prefer /utils/registration-code when available."
    ),
    deprecated=True,
    response_description="Single-use registration code payload.",
)
async def issue_owners_registration_code(db: Session = Depends(get_db)):
    code = mint_registration_code(db)
    db.commit()
    return {"registration_code": code}


@router.get(
    "/me",
    response_model=OwnerDetailResponse,
    summary="Get current owner profile",
    response_description="Authenticated owner with caller-visible zones and devices",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Authenticated owner was not found.",
        },
    },
)
async def get_current_owner(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get current authenticated owner."""
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )
    return OwnerDetailResponse.model_validate(_normalize_owner_name(owner))


@router.get(
    "/{owner_id}",
    response_model=OwnerDetailResponse,
    summary="Get owner by id",
    description="Get a profile only if it is visible under caller account visibility rules.",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Not authorized to view the requested owner.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Caller or requested owner was not found.",
        },
    },
    response_description="Requested owner profile.",
)
async def get_owner(
    owner_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get an owner by ID (requires authentication)."""
    caller = owner_crud.get_owner(db, current_user["user_id"])
    if not caller:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caller not found",
        )
    allowed_ids = visible_owner_ids(db, caller, include_inactive=True)
    if owner_id not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to view this owner",
        )

    owner = owner_crud.get_owner(db, owner_id)
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )
    return OwnerDetailResponse.model_validate(_normalize_owner_name(owner))


@router.get(
    "/",
    response_model=list[OwnerListResponse],
    summary="List visible owners",
    description=(
        "List owners visible to caller by account policy. Administrators see all "
        "owners in their account; users see only themselves."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Authenticated caller was not found.",
        },
    },
    response_description="Caller-visible owner list.",
)
async def list_owners(
    skip: int = 0,
    limit: int = 100,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List caller-visible owners."""
    caller = owner_crud.get_owner(db, current_user["user_id"])
    if not caller:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caller not found",
        )

    allowed_ids = messaging_visible_owner_ids(db, caller, include_inactive=True)
    owners = owner_crud.list_owners(db, skip=skip, limit=limit)
    owners = [owner for owner in owners if owner.id in allowed_ids]
    return [OwnerListResponse.model_validate(_normalize_owner_name(owner)) for owner in owners]


@router.patch(
    "/{owner_id}",
    response_model=OwnerResponse,
    summary="Update owner profile",
    description=(
        "Update owner profile fields. Administrators may update linked users, including "
        "active status toggles. Non-administrator callers may update only their own profile "
        "and cannot change active state."
    ),
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Not authorized to update the requested owner.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Caller or requested owner was not found.",
        },
    },
    response_description="Updated owner profile.",
)
async def update_owner(
    owner_id: int,
    owner_update: OwnerUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update an owner."""
    caller = owner_crud.get_owner(db, current_user["user_id"])
    if not caller:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caller not found",
        )

    is_admin = caller.role.value == "administrator"
    if not is_admin and current_user["user_id"] != owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to update this owner",
        )

    if owner_update.active is not None and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can change active status",
        )

    if owner_update.zone_id is not None:
        target = owner_crud.get_owner(db, owner_id)
        if not target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Owner not found",
            )
        if current_user["user_id"] != owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to update this owner's network ID",
            )
        assert_owner_may_edit_network_id(target)

    if is_admin and current_user["user_id"] != owner_id:
        allowed_ids = visible_owner_ids(db, caller, include_inactive=True)
        if owner_id not in allowed_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to update this owner",
            )

    updated_owner = owner_crud.update_owner(db, owner_id, owner_update)
    if not updated_owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )
    
    db.commit()
    return OwnerResponse.model_validate(_normalize_owner_name(updated_owner))


@router.delete(
    "/{owner_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete owner account",
    description="Delete the caller-owned account record.",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Not authorized to delete this owner.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Owner not found.",
        },
    },
    response_description="Owner deleted successfully.",
)
async def delete_owner(
    owner_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete an owner."""
    if current_user["user_id"] != owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to delete this owner",
        )
    
    deleted = owner_crud.delete_owner(db, owner_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Owner not found",
        )
    
    db.commit()
