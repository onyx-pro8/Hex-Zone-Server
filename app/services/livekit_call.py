"""Mint a LiveKit join token for one party of a guest call.

The API secret never leaves this process. Each call is its own room so two
calls cannot hear each other. Guest and admin receive different identities.
"""

from __future__ import annotations

from typing import Literal

from app.core.config import settings
from app.services.guest_call_service import GuestCallSession


def livekit_configured() -> bool:
    return bool(
        settings.LIVEKIT_URL.strip()
        and settings.LIVEKIT_API_KEY.strip()
        and settings.LIVEKIT_API_SECRET.strip()
    )


def room_name_for_call(call_id: str) -> str:
    return f"hz-{str(call_id or '').strip()}"


def mint_call_token(
    call: GuestCallSession,
    *,
    role: Literal["guest", "admin"],
) -> dict[str, str] | None:
    """Return ``server_url`` and ``participant_token``, or None when unset."""
    if call.state != "connected":
        return None
    if not livekit_configured():
        return None

    if role == "guest":
        identity = f"guest-{call.guest_id}".strip()
        name = (call.guest_name or "Guest").strip() or "Guest"
    else:
        identity = f"admin-{call.admin_owner_id}"
        name = "Administrator"
    if not identity:
        return None

    from livekit import api

    token = (
        api.AccessToken(settings.LIVEKIT_API_KEY.strip(), settings.LIVEKIT_API_SECRET.strip())
        .with_identity(identity)
        .with_name(name[:128])
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name_for_call(call.call_id),
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )
    return {
        "server_url": settings.LIVEKIT_URL.strip(),
        "participant_token": token,
    }
