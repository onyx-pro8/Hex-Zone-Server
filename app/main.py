"""Main FastAPI application."""
import logging
import threading
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from app.core.config import settings
from app.database import init_db
from app.routers import access, devices, guest, message_feature, messages, owners, utils, zones
from app.routes.contract_routes import router as contract_router
from app.utils.api_response import error_response
from app.websocket.routes import router as websocket_router

logging.basicConfig(level=logging.INFO)


def _init_db_background() -> None:
    """Run DB bootstrap without blocking app startup."""
    try:
        init_db()
        logging.info("Database initialized")
    except Exception as exc:
        logging.exception("Database initialization failed in background: %s", exc)

# Lifespan context
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of the app."""
    # Startup
    print("Starting Zone Weaver backend...")
    threading.Thread(target=_init_db_background, daemon=True).start()
    print("Database initialization started in background")
    yield
    # Shutdown
    print("Shutting down Zone Weaver backend...")


# Create FastAPI app
OPENAPI_TAGS = [
    {
        "name": "health",
        "description": "Service readiness and API discovery endpoints.",
    },
    {
        "name": "owners",
        "description": (
            "Registration, login, and owner profile management. Public GET "
            "/owners/registration-code issues administrator signup codes; POST /owners/register "
            "requires registration_code for administrator role. Exclusive accounts do not "
            "allow user-member registrations. Administrators can activate/deactivate linked users."
        ),
    },
    {
        "name": "zones",
        "description": (
            "Main Zone and optional Zone #2/#3 management. Includes Zone Matching, "
            "H3/grid, geofence, and related zone configuration payloads. Administrators can "
            "create only one Main Zone; users can create up to two zones. Zone listing follows "
            "role-aware visibility (admins see account zones, users see own zones plus admin main zone)."
        ),
    },
    {
        "name": "devices",
        "description": (
            "Device enrollment, presence heartbeat, and location updates. Device capacity is "
            "enforced by account tier per owner: private/exclusive/enhanced=1, private_plus=10, "
            "enhanced_plus=unlimited. Administrators can manage linked users' device active state."
        ),
    },
    {
        "name": "messages",
        "description": (
            "Member zone messaging. **`POST /messages`** creates **`Message`** rows for normal member↔member types, "
            "or **`ZoneMessageEvent`** when **`guest_id`** + **`zone_id`** are sent (**PERMISSION** / **CHAT** to QR guests; "
            "same persistence as **`GET /api/guest/messages`**). "
            "Guest-thread payloads accept **`type`** or **`message_type`** (see **`ZoneMessageCreate`**). "
            "**`GET /messages`** lists **`Message`** history for the caller."
        ),
    },
    {
        "name": "utilities",
        "description": (
            "Helper endpoints for H3 conversion, **member/account QR invite** (`POST /utils/qr/generate` "
            "+ join), and public issuance of single-use administrator registration codes "
            "(GET /utils/registration-code). "
            "**Door / guest access** URLs live under **`access`** (`GET /api/access/qr-link`), not here."
        ),
    },
    {
        "name": "message-feature",
        "description": (
            "Authenticated geo propagation, message blocking, **access schedules** "
            "(`/message-feature/access/schedules`), **guest arrival history** "
            "(`GET /message-feature/access/guest-requests`), **guest approve/reject** "
            "(`POST /message-feature/access/guest-requests/{guest_id}/approve|reject` — path **guest_id** only; "
            "**zone_id** inferred from the session), **PERMISSION** propagation for logged-in devices "
            "(`/message-feature/access/permission`). Public QR guests without JWT use **`access`** "
            "(`POST /api/access/permission`)."
        ),
    },
    {
        "name": "contract",
        "description": (
            "Mobile app contract routes aligned to setup wizard flows (register, zone "
            "setup, schedule access, request access, and notifications)."
        ),
    },
    {
        "name": "access",
        "description": (
            "Public guest entry for **zone QR scans** (no JWT): `POST /api/access/permission`, "
            "`GET /api/access/session/{guest_id}` (**`zone_id`** query recommended; optional — guest id alone resolves). "
            "After admin **APPROVED**, poll may return **`exchange_code`** → **`POST /api/access/guest-session`** (no Bearer) "
            "for a short-lived guest JWT, then **`/api/guest/*`** (see **API.md**). "
            "**Permission** response includes **`zone_id`** for clients that opened only **`?gt=`**. "
            "**Administrators** mint DB-backed tokens (`POST /api/access/qr-tokens`, SPA **`/access?gt=&zid=`**, optional **`eid`**) "
            "or static links (`GET /api/access/qr-link`, **`/access?zid=`**); optional server PNG QR. "
            "**Members** list guest arrivals (**Swagger:** **`access_list_guest_requests`**): **`GET /api/access/guest-requests?zone_id=`** "
            "(Bearer, **`GuestRequestListEnvelope`**). "
            "Approve/reject unexpected guests: `POST /api/access/approve|reject` (**JSON**: **guest_id**, **zone_id**) "
            "or **`POST /message-feature/access/guest-requests/{guest_id}/approve|reject`** (path **guest_id**; zone inferred). "
            "Bearer JWT for both families. "
            "Not member-invite (`/utils/qr/generate`)."
        ),
    },
    {
        "name": "guest",
        "description": (
            "**Approved anonymous guest** APIs. Obtain **`access_token`** from **`POST /api/access/guest-session`** "
            "(one-time **`exchange_code`** from **`GET /api/access/session/{guest_id}`** when **APPROVED**). "
            "Send **`Authorization: Bearer <access_token>`** — JWT claim **`token_use`** is **`guest_access`**; "
            "do not use this token on owner/member routes.\n\n"
            "**Endpoints:** **`GET /api/guest/me`**, **`GET /api/guest/zones/{zone_id}/peers`**, "
            "**`GET /api/guest/zones/{zone_id}/dashboard`**, **`GET|POST /api/guest/messages`** "
            "(only **PERMISSION** and **CHAT** types). v1 uses REST polling; WebSocket for guests is optional later. "
            "Contract details: **`API.md`**."
        ),
    },
]

app = FastAPI(
    title=settings.API_TITLE,
    description=(
        f"{settings.API_DESCRIPTION}\n\n"
        "This API supports setup wizard flows for administrator and user onboarding, "
        "including registration, account login, zone provisioning, access scheduling, "
        "QR-based onboarding, and zone messaging.\n\n"
        "Primary flow references:\n"
        "- Administrator registration: registration code + account + Main Zone + access-point setup. "
        "Fetch a code with GET /utils/registration-code (preferred) or GET /owners/registration-code, "
        "then send it as registrationCode on POST /register or registration_code on POST /owners/register. "
        "The tier code FREE is also accepted for administrators without calling GET (stateless).\n"
        "- User registration: account + optional Zone #2/#3 + schedule access + request access "
        "(no registration code required).\n"
        "- Login: email/username and password authentication.\n"
        "- **QR guest access (no login):** SPA **`/access?zid=`** (static) or **`/access?gt=&zid=`** (issued token; legacy **`gt`**-only URLs still work); "
        "guest submits name → `POST /api/access/permission` (response includes **`zone_id`**). "
        "Poll **`GET /api/access/session/{guest_id}`** with **`?zone_id=`** from URL or permission body, or omit **`zone_id`** to resolve by guest id. "
        "Administrators mint tokens with **`POST /api/access/qr-tokens`** or static **`GET /api/access/qr-link`**. "
        "**Members** list arrivals with **`GET /api/access/guest-requests?zone_id=`** (Bearer); use returned **`guest_id`** on **`POST /messages`** (**PERMISSION**/**CHAT**, **`type`** or **`message_type`**). "
        "Members create expectations via `/message-feature/access/schedules`; unexpected visits notify "
        "via WebSocket `unexpected_guest` / `guest_is_here`. "
        "Admins resolve pending unexpected visits with **`POST /api/access/approve|reject`** or **`POST /message-feature/access/guest-requests/{guest_id}/approve|reject`**. "
        "**Member invite QR** is separate: `POST /utils/qr/generate`."
    ),
    version=settings.API_VERSION,
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=86400,
)

@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logging.exception("Unhandled error processing request %s %s", request.method, request.url)
    return JSONResponse(status_code=500, content=error_response("Internal server error"))


@app.exception_handler(HTTPException)
async def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
    _ = request
    detail = exc.detail
    if isinstance(detail, dict):
        message = str(detail.get("message") or "Request failed")
        error_code = str(detail.get("error_code") or f"HTTP_{exc.status_code}")
        details = detail.get("details")
        err_obj = detail.get("error")
    else:
        message = str(detail) if detail else "Request failed"
        error_code = f"HTTP_{exc.status_code}"
        details = None
        err_obj = None

    payload = {
        "status": "error",
        "message": message,
        "error_code": error_code,
    }
    if err_obj is not None and isinstance(err_obj, dict):
        payload["error"] = err_obj
    else:
        payload["error"] = {"message": message}
    if details is not None:
        payload["details"] = details
    return JSONResponse(status_code=exc.status_code, content=payload)

# Include routers
app.include_router(owners.router)
app.include_router(devices.router)
app.include_router(zones.router)
app.include_router(messages.router)
app.include_router(utils.router)
app.include_router(message_feature.router)
app.include_router(access.router)
app.include_router(guest.router)
app.include_router(contract_router)
app.include_router(websocket_router)


@app.get("/", tags=["health"])
async def root():
    """Root endpoint."""
    return {
        "message": "Zone Weaver API",
        "version": settings.API_VERSION,
        "docs": "/docs",
    }


@app.get("/health", tags=["health"])
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
