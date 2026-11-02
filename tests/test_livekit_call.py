"""LiveKit join tokens are per call and per party."""

import base64
import json

from app.core.config import settings
from app.services.guest_call_service import GuestCallSession
from app.services.livekit_call import mint_call_token, room_name_for_call


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _connected() -> GuestCallSession:
    return GuestCallSession(
        call_id="abc123",
        zone_id="zone-1",
        guest_id="guest-9",
        guest_name="Ada Guest",
        admin_owner_id=42,
        caller_role="guest",
        state="connected",
    )


def test_room_name_is_the_call(monkeypatch):
    monkeypatch.setattr(settings, "LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setattr(settings, "LIVEKIT_API_KEY", "APItestkeyvalue")
    monkeypatch.setattr(settings, "LIVEKIT_API_SECRET", "test-secret-that-is-long-enough-for-hs256")
    call = _connected()
    assert room_name_for_call(call.call_id) == "hz-abc123"
    guest = mint_call_token(call, role="guest")
    admin = mint_call_token(call, role="admin")
    assert guest and admin
    assert guest["server_url"] == "wss://example.livekit.cloud"
    assert guest["participant_token"] != admin["participant_token"]
    guest_claims = _claims(guest["participant_token"])
    admin_claims = _claims(admin["participant_token"])
    assert guest_claims["video"]["room"] == "hz-abc123"
    assert admin_claims["video"]["room"] == "hz-abc123"
    assert guest_claims["video"]["roomJoin"] is True
    assert guest_claims["video"]["canPublish"] is True
    assert guest_claims["sub"] == "guest-guest-9"
    assert admin_claims["sub"] == "admin-42"


def test_ringing_call_gets_no_token(monkeypatch):
    monkeypatch.setattr(settings, "LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setattr(settings, "LIVEKIT_API_KEY", "APItestkeyvalue")
    monkeypatch.setattr(settings, "LIVEKIT_API_SECRET", "test-secret-that-is-long-enough-for-hs256")
    call = _connected()
    call.state = "ringing"
    assert mint_call_token(call, role="guest") is None
