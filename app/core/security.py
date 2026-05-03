"""Security utilities for authentication and authorization."""
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

import secrets

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# Missing `Authorization` should yield **401** (not FastAPI's default **403** from auto_error).
security = HTTPBearer(auto_error=False)


def get_password_hash(password: str) -> str:
    """Hash a password."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    return pwd_context.verify(plain_password, hashed_password)


def create_guest_access_token(
    *,
    guest_id: str,
    zone_ids: list[str],
    expires_delta: Optional[timedelta] = None,
) -> tuple[str, int, datetime]:
    """Mint a short-lived JWT for approved anonymous guests (`/api/guest/*` only)."""
    minutes = max(1, int(settings.GUEST_ACCESS_TOKEN_EXPIRE_MINUTES))
    delta = expires_delta or timedelta(minutes=minutes)
    expire = datetime.utcnow() + delta
    to_encode: dict[str, Any] = {
        "sub": f"guest:{guest_id}",
        "token_use": "guest_access",
        "typ": "guest_access",
        "zone_ids": zone_ids,
        "allowed_message_types": ["PERMISSION", "CHAT"],
        "iat": datetime.utcnow(),
        "exp": expire,
        "jti": str(uuid.uuid4()),
    }
    encoded = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded, int(delta.total_seconds()), expire


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def verify_token(token: str) -> dict:
    """Verify a JWT token and return payload."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )


async def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    """Dependency to get current user from token."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    token = credentials.credentials
    payload = verify_token(token)
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
    try:
        uid = int(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        ) from exc
    return {"user_id": uid}


async def get_current_guest(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Bearer JWT from **`POST /api/access/guest-session`** (`token_use` **guest_access**)."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    token = credentials.credentials
    payload = verify_token(token)
    if payload.get("token_use") != "guest_access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "Invalid guest token.", "error_code": "INVALID_GUEST_TOKEN"},
        )
    sub = str(payload.get("sub") or "")
    if not sub.startswith("guest:"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "Invalid guest token.", "error_code": "INVALID_GUEST_TOKEN"},
        )
    guest_id = sub.split(":", 1)[1].strip()
    if not guest_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "Invalid guest token.", "error_code": "INVALID_GUEST_TOKEN"},
        )
    zone_ids = payload.get("zone_ids") or []
    if not isinstance(zone_ids, list):
        zone_ids = []
    zone_ids = [str(z).strip() for z in zone_ids if str(z).strip()]
    exp_raw = payload.get("exp")
    expires_at: str | None = None
    if isinstance(exp_raw, (int, float)):
        expires_at = datetime.utcfromtimestamp(int(exp_raw)).replace(microsecond=0).isoformat() + "Z"
    return {
        "guest_id": guest_id,
        "zone_ids": zone_ids,
        "allowed_message_types": list(payload.get("allowed_message_types") or ["PERMISSION", "CHAT"]),
        "jti": payload.get("jti"),
        "expires_at": expires_at,
    }


def generate_api_key() -> str:
    """Generate a random API key."""
    return secrets.token_urlsafe(32)


def generate_qr_token() -> str:
    """Generate a QR registration token."""
    return secrets.token_urlsafe(16)


def generate_registration_code_token() -> str:
    """Generate a server-issued registration code for setup wizard flows."""
    return secrets.token_urlsafe(24)
