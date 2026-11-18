"""Subscription entitlements, Stripe Checkout / Customer Portal, webhooks."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.crud import owner as owner_crud
from app.database import get_db
from app.core.security import get_current_user
from app.services import subscription_service

router = APIRouter(prefix="/subscription", tags=["subscription"])


class CheckoutBody(BaseModel):
    plan_key: str = Field(..., description="e.g. enhanced, private_plus, enhanced_plus_2")
    billing_interval: str = Field("monthly", description="monthly | annual")
    success_url: str | None = Field(
        None,
        description="Optional web/app return URL after paid Checkout (may include {CHECKOUT_SESSION_ID})",
    )
    cancel_url: str | None = Field(
        None,
        description="Optional web/app return URL when Checkout is cancelled",
    )


class PortalBody(BaseModel):
    return_url: str | None = Field(
        None,
        description="Optional web/app URL after leaving the Stripe Customer Portal",
    )


class ConfirmBody(BaseModel):
    session_id: str = Field(..., min_length=8, description="Stripe Checkout Session id (cs_...)")


@router.get(
    "/me",
    summary="Current network subscription + feature usage",
)
async def get_my_subscription(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    return subscription_service.build_subscription_overview(db, owner)


@router.get(
    "/plans",
    summary="Public plan catalog",
)
async def list_plans():
    return {"plans": subscription_service.list_public_plans()}


@router.post(
    "/checkout",
    summary="Create Stripe Checkout session (network admin)",
)
async def create_checkout(
    body: CheckoutBody,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    return subscription_service.create_checkout_session(
        db,
        caller=owner,
        plan_key=body.plan_key,
        billing_interval=body.billing_interval,
        success_url=body.success_url,
        cancel_url=body.cancel_url,
    )


@router.post(
    "/portal",
    summary="Create Stripe Customer Portal session (network admin)",
)
async def create_portal(
    body: PortalBody | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    return subscription_service.create_billing_portal_session(
        db,
        caller=owner,
        return_url=(body.return_url if body else None),
    )


@router.post(
    "/confirm",
    summary="Confirm Checkout session after deep-link return",
)
async def confirm_checkout(
    body: ConfirmBody,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Applies plan from Stripe even when webhooks are not configured yet."""
    owner = owner_crud.get_owner(db, current_user["user_id"])
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
    return subscription_service.confirm_checkout_session(
        db,
        caller=owner,
        session_id=body.session_id,
    )


@router.post(
    "/webhook",
    summary="Stripe webhook receiver",
    include_in_schema=False,
)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    return subscription_service.handle_stripe_webhook(db, payload, signature)
