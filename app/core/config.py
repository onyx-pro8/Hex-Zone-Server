"""Configuration management for Zone Weaver backend."""
from typing import Optional
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


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
    # Two ways to configure the connection:
    #   1. Set the full DATABASE_URL (port may be embedded as host:PORT/db).
    #   2. Set discrete DB_* components (DB_HOST takes precedence when present).
    # When DB_PORT is set in the environment it always defines/overrides the
    # connection port (even if DATABASE_URL embeds a different one). When left
    # unset, the URL's own port is kept, falling back to 5432. Read
    # `sqlalchemy_database_url` (not DATABASE_URL directly) to get the resolved
    # connection string.
    DATABASE_URL: str = "postgresql+psycopg2://zoneweaver_db_user:FJdjpGjfLn4Fa9VfM2FCRXSfX13jg2rk@dpg-d7bscjggjchc73fhscf0-a.oregon-postgres.render.com/zoneweaver_db_5isc"
    DB_DRIVER: str = "postgresql+psycopg2"
    DB_HOST: str = ""
    DB_PORT: Optional[int] = None
    DB_USER: str = ""
    DB_PASSWORD: str = ""
    DB_NAME: str = ""

    # JWT
    SECRET_KEY: str = "your-secret-key-change-in-production-minimum-32-chars-required"
    ALGORITHM: str = "HS256"
    # 7 days. Keeps members signed in between sessions so a momentary token
    # expiry (e.g. while composing a message) no longer forces a re-login.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080

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

    # Geocoding / area boundaries (OpenStreetMap Nominatim)
    GEOCODING_PROVIDER: str = "nominatim"
    BOUNDARY_LOOKUP_ENABLED: bool = True
    NOMINATIM_USER_AGENT: str = "HexZone/1.0 (https://hex-zone.local; area-boundary-lookup)"

    # Mobile push (optional). FCM legacy server key; leave empty to skip FCM sends.
    FCM_SERVER_KEY: str = ""
    APNS_HTTP_URL: str = ""
    APNS_AUTH_KEY: str = ""
    # Expo Push HTTP/2 access token (optional - only needed if your Expo project
    # enforces the "Enhanced Security for Push Notifications" setting).
    EXPO_ACCESS_TOKEN: str = ""

    # Devices marked online but without a heartbeat within this window are treated
    # as offline for session conflict checks and claim-session flows.
    DEVICE_PRESENCE_TIMEOUT_SECONDS: int = 1800

    UNKNOWN_MESSAGE_RATE_LIMIT_SECONDS: int = 10
    # SENSOR telemetry can be high-frequency; throttle repeat sends per sender.
    SENSOR_MESSAGE_RATE_LIMIT_SECONDS: int = 5

    # PANIC / NS_PANIC are MAX priority: keep re-sending the mobile push in the
    # background until every recipient token reports delivered (bounded).
    PANIC_PUSH_RETRY_MAX_ATTEMPTS: int = 4
    PANIC_PUSH_RETRY_DELAY_SECONDS: int = 15

    # WELLNESS_CHECK reminders: a background job re-pushes recipients who have
    # not acknowledged after the delay, up to a capped number of reminders.
    WELLNESS_REMINDER_ENABLED: bool = True
    WELLNESS_REMINDER_DELAY_SECONDS: int = 300
    WELLNESS_REMINDER_MAX: int = 3
    WELLNESS_REMINDER_SCAN_INTERVAL_SECONDS: int = 120
    WELLNESS_REMINDER_LOOKBACK_HOURS: int = 24

    # Registration code HMAC + email delivery (administrator self-service signup).
    # When REGISTRATION_CODE_HMAC_SECRET is empty, the runtime falls back to SECRET_KEY.
    REGISTRATION_CODE_HMAC_SECRET: str = ""
    REGISTRATION_CODE_EMAIL_FROM_NAME: str = "Hex Zone"

    # Public-facing support contact, included in REG-CODE issuance emails / API response.
    SUPPORT_CONTACT_NAME: str = "Hex Zone Support"
    SUPPORT_CONTACT_EMAIL: str = "support@zoneweaver.com"
    SUPPORT_CONTACT_PHONE: str = "+1 (555) 010-0123"
    SUPPORT_CONTACT_WEBSITE: str = "https://zoneweaver.com"

    # Outbound SMTP (Resend.com is the reference provider; any SMTP server works).
    # Leave SMTP_HOST empty to disable real delivery — issuance still works and the
    # email payload is logged so the code can be retrieved during local development.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_FROM: str = ""
    SMTP_USE_SSL: bool = True
    SMTP_TIMEOUT_SECONDS: int = 15

    @property
    def sqlalchemy_database_url(self) -> str:
        """Resolved DB connection string with an explicit port.

        If discrete ``DB_HOST`` is set, the URL is built from the ``DB_*``
        components (``DB_PORT`` or 5432). Otherwise ``DATABASE_URL`` is used:
        when ``DB_PORT`` is provided it overrides any embedded port, and when
        it is unset the URL's own port is kept (falling back to 5432).
        """
        if self.DB_HOST:
            password = quote_plus(self.DB_PASSWORD)
            port = self.DB_PORT or 5432
            return (
                f"{self.DB_DRIVER}://{self.DB_USER}:{password}"
                f"@{self.DB_HOST}:{port}/{self.DB_NAME}"
            )

        url = make_url(self.DATABASE_URL)
        if self.DB_PORT is not None:
            url = url.set(port=self.DB_PORT)
        elif url.port is None:
            url = url.set(port=5432)
        return url.render_as_string(hide_password=False)


settings = Settings()
