"""Forgot-password and signed-in password change."""
import asyncio

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import require_auth
from app.models.owner import Owner
from app.services import password_reset_service
from app.utils.api_response import success_response

router = APIRouter(tags=["auth"])


class PasswordResetEmailRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=8)


class PasswordResetCompleteRequest(BaseModel):
    email: EmailStr
    reset_token: str = Field(min_length=10, validation_alias="resetToken")
    password: str = Field(min_length=8)

    model_config = {"populate_by_name": True}


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, validation_alias="currentPassword")
    new_password: str = Field(min_length=8, validation_alias="newPassword")

    model_config = {"populate_by_name": True}


@router.post(
    "/auth/password/forgot",
    summary="Email a 4-digit password reset code",
)
async def start_password_reset(
    payload: PasswordResetEmailRequest,
    db: Session = Depends(get_db),
):
    data = await asyncio.to_thread(
        password_reset_service.start_password_reset,
        db,
        str(payload.email),
    )
    db.commit()
    return success_response(data)


@router.post(
    "/auth/password/forgot/resend",
    summary="Resend the password reset code",
)
async def resend_password_reset(
    payload: PasswordResetEmailRequest,
    db: Session = Depends(get_db),
):
    data = await asyncio.to_thread(
        password_reset_service.resend_password_reset,
        db,
        str(payload.email),
    )
    db.commit()
    return success_response(data)


@router.post(
    "/auth/password/forgot/confirm",
    summary="Confirm the password reset code",
)
async def confirm_password_reset(
    payload: PasswordResetConfirmRequest,
    db: Session = Depends(get_db),
):
    data = await asyncio.to_thread(
        password_reset_service.confirm_password_reset,
        db,
        str(payload.email),
        payload.code,
    )
    db.commit()
    return success_response(data)


@router.post(
    "/auth/password/reset",
    summary="Set a new password with the reset token",
)
async def complete_password_reset(
    payload: PasswordResetCompleteRequest,
    db: Session = Depends(get_db),
):
    data = await asyncio.to_thread(
        password_reset_service.complete_password_reset,
        db,
        str(payload.email),
        payload.reset_token,
        payload.password,
    )
    db.commit()
    return success_response(data)


@router.post(
    "/auth/password/change",
    summary="Change the signed-in account password",
    status_code=status.HTTP_200_OK,
)
async def change_password(
    payload: PasswordChangeRequest,
    owner: Owner = Depends(require_auth),
    db: Session = Depends(get_db),
):
    data = await asyncio.to_thread(
        password_reset_service.change_password,
        db,
        owner,
        payload.current_password,
        payload.new_password,
    )
    db.commit()
    return success_response(data)
