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
            "Member zone messaging (Bearer **member** JWT — numeric `sub`). **`POST /messages`** creates **`Message`** rows "
            "for member↔member chat, or **`ZoneMessageEvent`** when **`guest_id`** + **`zone_id`** are set (**Access** channel: "
            "**PERMISSION** / **CHAT** to approved QR guests; same store as **`GET /api/guest/messages`**). "
            "Member→guest: send **`message`**, **`type`** or **`message_type`**, **`visibility`** (often **`private`**), **`zone_id`**/**`zoneId`**, **`guest_id`**; omit **`receiver_id`**. "
            "See schema **`ZoneMessageCreate`** (Swagger **Schemas**). **`GET /messages`** defaults to **`Message`** history; "
            "with **`guest_id`**/**`guestId`**, **`zone_id`**/**`zoneId`**, and/or **`requestId`** (session **`id`** or guest UUID) "
            "it lists the same **`ZoneMessageEvent`** access thread **`GET /api/guest/messages`** uses (**PERMISSION** + **CHAT**)."
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
            "(`/message-feature/access/schedules`). **Guest roster (canonical Swagger):** "
            "**`GET /api/access/guest-requests`** — this tag still exposes "
            "**`GET /message-feature/access/guest-requests`** (same rows as raw JSON array) and "
            "**`POST /message-feature/access/guest-requests/{guest_id}/approve|reject`** (path **guest_id**; "
            "**zone_id** inferred). **PERMISSION** for logged-in devices: **`/message-feature/access/permission`**. "
            "Anonymous door guests use **`access`**: **`POST /api/access/permission`**."
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
            "**Anonymous + member** guest-access routes under **`/api/access`**. "
            "**No JWT:** **`POST /api/access/permission`**, **`GET /api/access/session/{guest_id}`**, **`POST /api/access/guest-session`**. "
            "Poll response includes **`status`** (EXPECTED | UNEXPECTED | APPROVED | REJECTED) and **`approval_status`** "
            "(PENDING | APPROVED | REJECTED) for dashboards; when approved, **`exchange_code`** + **`exchange_expires_at`** enable the guest JWT exchange. "
            "**Member JWT:** **`GET /api/access/guest-requests`** (operationId **`access_list_guest_requests`**), **`POST /api/access/approve|reject`**, QR helpers. "
            "**Permission** response echoes **`zone_id`** when the invite had only **`gt`**. "
            "**Administrators** mint stored tokens (**`POST /api/access/qr-tokens`**, SPA **`/access?gt=&zid=`**) or static **`GET /api/access/qr-link`**. "
            "Legacy duplicate listing: **`GET /message-feature/access/guest-requests`** (raw array). "
            "Not the member-account invite flow: **`POST /utils/qr/generate`**."
        ),
    },
    {
        "name": "guest",
        "description": (
            "**Guest JWT only** (`Authorization: Bearer`; claim **`token_use`=`guest_access`**, subject **`guest:{guest_id}`**). "
            "Obtain token from **`POST /api/access/guest-session`** using **`exchange_code`** from **`GET /api/access/session/{guest_id}`** after approval. "
            "Do **not** send the member (zoneweaver) token on these routes.\n\n"
            "| Endpoint | Purpose |\n"
            "|----------|--------|\n"
            "| **`GET /api/guest/me`** | Profile: **`guest_id`**, **`display_name`**, **`zone_ids`**, **`allowed_message_types`**, **`expires_at`** |\n"
            "| **`GET /api/guest/zones/{zone_id}/peers`** | Hosts/staff (**`owner_id`**) available for guest CHAT |\n"
            "| **`GET /api/guest/zones/{zone_id}/dashboard`** | Optional welcome copy and links |\n"
            "| **`GET /api/guest/messages`** | Thread with **`with_owner_id`** (member **`owners.id`**) |\n"
            "| **`POST /api/guest/messages`** | Guest → member **CHAT** only (**`to_owner_id`**) |\n\n"
            "Allowed message types are **CHAT** only in the minted JWT. Errors use **`{ \"status\":\"error\", \"message\", \"error_code\" }`**."
        ),
    },
]

_ACCESS_ZONE_CLIENT_DOC = """

### Hex Zone client (reference)

**Auth:** Member Bearer = **`zoneweaver_token`** stack (**`sub`** = owner id). Guest Bearer = separate token (**`zoneweaver_guest_access_token`**), **only** **`/api/guest/*`**.

**Typical Vite env (adjust per deploy):** `VITE_API_BASE_URL`, `VITE_GUEST_API_BASE_PATH` (default `/api/guest`), `VITE_GUEST_SESSION_EXCHANGE_URL` (`/api/access/guest-session`), `VITE_ADMIN_GUEST_REQUESTS_LIST_URL` (`/api/access/guest-requests`), `VITE_ACCESS_SESSION_URL_TEMPLATE`, `VITE_ANONYMOUS_ACCESS_PERMISSION_PATH` (`/api/access/permission`).
"""

app = FastAPI(
    title=settings.API_TITLE,
    contact={
        "name": "Zone Weaver / Hex Zone API",
    },
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
        "Poll **`GET /api/access/session/{guest_id}`** — response includes **`status`**, **`approval_status`**, and when approved **`exchange_code`** → **`POST /api/access/guest-session`**. "
        "Administrators mint tokens with **`POST /api/access/qr-tokens`** or static **`GET /api/access/qr-link`**. "
        "**Members** list arrivals with **`GET /api/access/guest-requests?zone_id=`** (Bearer); use returned **`guest_id`** on **`POST /messages`** (**PERMISSION**/**CHAT**, **`type`** or **`message_type`**). "
        "Members create expectations via `/message-feature/access/schedules`; unexpected visits notify "
        "via WebSocket `unexpected_guest` / `guest_is_here`. "
        "Admins resolve pending unexpected visits with **`POST /api/access/approve|reject`** or **`POST /message-feature/access/guest-requests/{guest_id}/approve|reject`**. "
        "**Member invite QR** is separate: `POST /utils/qr/generate`."
        f"{_ACCESS_ZONE_CLIENT_DOC}"
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
