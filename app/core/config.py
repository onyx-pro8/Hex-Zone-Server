"""Configuration management for Zone Weaver backend."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        # Environment variables always take precedence over .env file values,
        # which ensures the Railway-provided DATABASE_URL is used in production.
        extra="ignore",
    )

    # Database
    DATABASE_URL: str = "postgresql+psycopg2://zoneweaver_db_user:FJdjpGjfLn4Fa9VfM2FCRXSfX13jg2rk@dpg-d7bscjggjchc73fhscf0-a.oregon-postgres.render.com/zoneweaver_db_5isc"

    # JWT
    SECRET_KEY: str = "your-secret-key-change-in-production-minimum-32-chars-required"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # API
    API_TITLE: str = "Zone Weaver API"
    API_VERSION: str = "1.0.0"
    API_DESCRIPTION: str = "User-Defined Zone Message Distribution Platform"
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    MESSAGES_INBOX_MERGE_GUEST_ACCESS_CHAT: bool = Field(
        default=True,
        description=(
            "When **true**, **`GET /messages?owner_id=…`** merges Access-channel **`ZoneMessageEvent`** **`CHAT`** for thread parties "
            "with **`Message`** rows and **`PERMISSION`** audit lines (**OpenAPI**: **`messages` → GET /**). "
            "**false** disables only **CHAT** merge (guest **CHAT** remains on **`GET /api/guest/messages`** and on **`GET /messages`** when **`guest_id`** query scopes the Access thread)."
        ),
    )

    # Guest access QR: canonical SPA origin for `/access?zid=...` (no trailing slash).
    # Production/staging: set **GUEST_ACCESS_APP_BASE_URL** (e.g. https://app.example.com).
    # **PUBLIC_WEB_APP_URL** is a deprecated alias read when GUEST_ACCESS_APP_BASE_URL is empty.
    GUEST_ACCESS_APP_BASE_URL: str = ""
    PUBLIC_WEB_APP_URL: str = ""

    # Anonymous POST /api/access/permission: max requests per client IP per rolling minute.
    GUEST_ACCESS_PERMISSION_MAX_PER_MINUTE: int = 60

    # Approved-guest JWT exchange + token TTLs (see API.md).
    GUEST_ACCESS_EXCHANGE_TTL_MINUTES: int = 12
    GUEST_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    GUEST_ACCESS_GUEST_SESSION_MAX_PER_MINUTE: int = 30

    # H3
    H3_DEFAULT_RESOLUTION: int = 13
    H3_MIN_RESOLUTION: int = 0
    H3_MAX_RESOLUTION: int = 15

    # Zone capacity policy
    # Defaults keep current behavior (3 total zones) while reserving at least
    # one slot for standard users. Increase MAX_ZONES_TOTAL to 5 in future
    # deployments without code changes.
    MAX_ZONES_TOTAL: int = 3
    RESERVED_FOR_STANDARD_USERS: int = 1
    # Legacy setting retained for compatibility with older code paths.
    MAX_ZONES_PER_USER: int = 3
    REGISTRATION_CODE_EXPIRE_HOURS: int = 24

    # Geocoding (placeholder for future integration)
    GEOCODING_PROVIDER: str = "nominatim"


settings = Settings()
