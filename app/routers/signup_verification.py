"""Public signup email verification (4-digit code via Resend)."""
import asyncio

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.routes.contract_routes import RegisterRequest
from app.services import signup_verification_service
from app.utils.api_response import success_response

router = APIRouter(tags=["auth"])


class SignupCodeConfirmRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=8)


class SignupCodeResendRequest(BaseModel):
    email: EmailStr


@router.post(
    "/auth/signup/verification",
    summary="Email a 4-digit signup code",
    description=(
        "Validate a public create-account form, store it, and email a 4-digit "
        "code through Resend. The account is not created until the code is confirmed."
    ),
)
async def start_signup_verification(
    payload: RegisterRequest,
    db: Session = Depends(get_db),
):
    data = await asyncio.to_thread(
        signup_verification_service.start_signup_verification,
        db,
        payload.model_dump(),
    )
    db.commit()
    return success_response(data)


@router.post(
    "/auth/signup/verification/resend",
    summary="Resend the signup verification code",
)
async def resend_signup_verification(
    payload: SignupCodeResendRequest,
    db: Session = Depends(get_db),
):
    data = await asyncio.to_thread(
        signup_verification_service.resend_signup_verification,
        db,
        str(payload.email),
    )
    db.commit()
    return success_response(data)


@router.post(
    "/auth/signup/verification/confirm",
    status_code=status.HTTP_201_CREATED,
    summary="Confirm the signup code and create the account",
)
async def confirm_signup_verification(
    payload: SignupCodeConfirmRequest,
    db: Session = Depends(get_db),
):
    data = await asyncio.to_thread(
        signup_verification_service.confirm_signup_verification,
        db,
        str(payload.email),
        payload.code,
    )
    db.commit()
    return success_response(data)
