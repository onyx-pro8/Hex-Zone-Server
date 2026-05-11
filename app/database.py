"""Database connection and session management."""
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from app.core.config import settings

_db_url = settings.DATABASE_URL
_engine_kwargs = {
    "echo": False,
    "future": True,
    "pool_pre_ping": True,
}

if _db_url.startswith("postgresql"):
    _engine_kwargs["connect_args"] = {"connect_timeout": 10}
    _engine_kwargs["pool_recycle"] = 270
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10

# Create sync engine
engine = create_engine(_db_url, **_engine_kwargs)

# Create session factory
session_maker = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

# Base class for models
Base = declarative_base()
logger = logging.getLogger(__name__)


def get_db() -> Session:
    """Dependency: get database session."""
    db = session_maker()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize database tables."""
    import app.models  # noqa: F401

    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
    Base.metadata.create_all(bind=engine)

    if engine.dialect.name == "postgresql":
        # Run critical compatibility patches in an isolated transaction first.
        # The broader migration block below can fail on legacy enum/type drift;
        # this ensures core columns used by active request paths still exist.
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS scope VARCHAR(16);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_scope ON messages (scope);"))
            conn.execute(text("ALTER TABLE zone_message_events ADD COLUMN IF NOT EXISTS scope VARCHAR(16);"))
            conn.execute(text("ALTER TABLE zone_message_events ADD COLUMN IF NOT EXISTS receiver_id INTEGER;"))
            conn.execute(text("ALTER TABLE zone_message_events ADD COLUMN IF NOT EXISTS category VARCHAR(16);"))
            conn.execute(
                text(
                    "ALTER TABLE zone_message_events ADD COLUMN IF NOT EXISTS body JSONB DEFAULT '{}'::jsonb;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE guest_access_sessions ADD COLUMN IF NOT EXISTS qr_token_id INTEGER;"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_guest_access_sessions_qr_token_id ON guest_access_sessions (qr_token_id);"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE guest_access_qr_tokens "
                    "ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT FALSE;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE guest_access_qr_tokens "
                    "ADD COLUMN IF NOT EXISTS use_count INTEGER NOT NULL DEFAULT 0;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE guest_access_qr_tokens "
                    "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW();"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE guest_access_qr_tokens "
                    "ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP WITHOUT TIME ZONE;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE guest_access_qr_tokens "
                    "ALTER COLUMN expires_at DROP NOT NULL;"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_guest_access_qr_tokens_is_primary "
                    "ON guest_access_qr_tokens (is_primary);"
                )
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_guest_access_qr_tokens_active_primary_zone "
                    "ON guest_access_qr_tokens (zone_id) "
                    "WHERE revoked_at IS NULL AND is_primary IS TRUE;"
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS guest_access_qr_token_audits (
                        id SERIAL PRIMARY KEY,
                        token_id INTEGER NOT NULL REFERENCES guest_access_qr_tokens(id) ON DELETE CASCADE,
                        zone_id VARCHAR(100) NOT NULL,
                        action VARCHAR(32) NOT NULL,
                        actor_owner_id INTEGER NULL REFERENCES owners(id) ON DELETE SET NULL,
                        reason VARCHAR(255) NULL,
                        metadata_json JSONB NULL,
                        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                    );
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_guest_access_qr_token_audits_token_id "
                    "ON guest_access_qr_token_audits (token_id);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_guest_access_qr_token_audits_zone_id "
                    "ON guest_access_qr_token_audits (zone_id);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_guest_access_qr_token_audits_action "
                    "ON guest_access_qr_token_audits (action);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_guest_access_qr_token_audits_actor_owner_id "
                    "ON guest_access_qr_token_audits (actor_owner_id);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_guest_access_qr_token_audits_created_at "
                    "ON guest_access_qr_token_audits (created_at);"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE guest_access_sessions ADD COLUMN IF NOT EXISTS exchange_code VARCHAR(36);"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE guest_access_sessions ADD COLUMN IF NOT EXISTS exchange_expires_at TIMESTAMP;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE guest_access_sessions ADD COLUMN IF NOT EXISTS exchange_consumed_at TIMESTAMP;"
                )
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_guest_access_sessions_exchange_code "
                    "ON guest_access_sessions (exchange_code) WHERE exchange_code IS NOT NULL;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE zone_message_events ADD COLUMN IF NOT EXISTS sender_guest_id VARCHAR(36);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_zone_message_events_sender_guest_id "
                    "ON zone_message_events (sender_guest_id);"
                )
            )
            conn.execute(
                text(
                    """
                    ALTER TABLE zone_message_events
                    ADD COLUMN IF NOT EXISTS guest_access_session_id INTEGER
                    REFERENCES guest_access_sessions(id) ON DELETE SET NULL;
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_zone_message_events_guest_access_session_id "
                    "ON zone_message_events (guest_access_session_id);"
                )
            )

        with engine.begin() as conn:
            # Backward-compatible schema patch for older deployments missing owners.zone_id.
            conn.execute(text("ALTER TABLE owners ADD COLUMN IF NOT EXISTS zone_id VARCHAR(100);"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_owner_zone_id ON owners (zone_id);"))
            conn.execute(
                text(
                    """
                    UPDATE owners
                    SET zone_id = CONCAT('owner-', id::text)
                    WHERE zone_id IS NULL OR zone_id = '';
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_message_type ON messages (message_type);"))
            conn.execute(
                text(
                    """
                    UPDATE messages
                    SET message_type = CASE
                        WHEN message_type = 'NORMAL' THEN 'SERVICE'
                        WHEN message_type = 'PANIC' THEN 'PANIC'
                        WHEN message_type = 'NS_PANIC' THEN 'NS_PANIC'
                        WHEN message_type = 'SENSOR' THEN 'SENSOR'
                        ELSE COALESCE(message_type, 'SERVICE')
                    END;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE messages
                    SET scope = CASE
                        WHEN message_type IN ('PRIVATE', 'PERMISSION', 'CHAT') THEN 'private'
                        ELSE 'public'
                    END
                    WHERE scope IS NULL OR scope = '';
                    """
                )
            )
            try:
                conn.execute(
                    text(
                        "UPDATE messages SET visibility = scope "
                        "WHERE visibility IS DISTINCT FROM scope::messagevisibility;"
                    )
                )
            except Exception as exc:
                logger.warning("Skipping legacy visibility backfill: %s", exc)
            conn.execute(
                text(
                    """
                    UPDATE zone_message_events
                    SET type = CASE WHEN type::text = 'NORMAL' THEN 'SERVICE' ELSE type::text END::text;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE zone_message_events
                    SET category = CASE
                        WHEN type::text IN ('SENSOR','PANIC','NS_PANIC','UNKNOWN') THEN 'Alarm'
                        WHEN type::text IN ('PRIVATE','PA','SERVICE','WELLNESS_CHECK') THEN 'Alert'
                        WHEN type::text IN ('PERMISSION','CHAT') THEN 'Access'
                        ELSE 'Alert'
                    END
                    WHERE category IS NULL OR category = '';
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE zone_message_events
                    SET scope = CASE
                        WHEN type::text IN ('PRIVATE','PERMISSION','CHAT') THEN 'private'
                        ELSE 'public'
                    END
                    WHERE scope IS NULL OR scope = '';
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_zone_message_events_type ON zone_message_events (type);"))
            conn.execute(
                text(
                    """
                    UPDATE owners
                    SET last_name = COALESCE(NULLIF(first_name, ''), 'User')
                    WHERE last_name IS NULL OR last_name = '';
                    """
                )
            )
            conn.execute(text("ALTER TABLE owners ALTER COLUMN zone_id SET NOT NULL;"))
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'accounttype') THEN
                            ALTER TYPE accounttype ADD VALUE IF NOT EXISTS 'private_plus';
                            ALTER TYPE accounttype ADD VALUE IF NOT EXISTS 'enhanced';
                            ALTER TYPE accounttype ADD VALUE IF NOT EXISTS 'enhanced_plus';
                        END IF;
                    END$$;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ownerrole') THEN
                            CREATE TYPE ownerrole AS ENUM ('ADMINISTRATOR', 'USER');
                        END IF;

                        IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ownerrole') THEN
                            IF EXISTS (
                                SELECT 1
                                FROM pg_enum e
                                JOIN pg_type t ON t.oid = e.enumtypid
                                WHERE t.typname = 'ownerrole' AND e.enumlabel = 'administrator'
                            ) AND NOT EXISTS (
                                SELECT 1
                                FROM pg_enum e
                                JOIN pg_type t ON t.oid = e.enumtypid
                                WHERE t.typname = 'ownerrole' AND e.enumlabel = 'ADMINISTRATOR'
                            ) THEN
                                ALTER TYPE ownerrole RENAME VALUE 'administrator' TO 'ADMINISTRATOR';
                            END IF;

                            IF EXISTS (
                                SELECT 1
                                FROM pg_enum e
                                JOIN pg_type t ON t.oid = e.enumtypid
                                WHERE t.typname = 'ownerrole' AND e.enumlabel = 'user'
                            ) AND NOT EXISTS (
                                SELECT 1
                                FROM pg_enum e
                                JOIN pg_type t ON t.oid = e.enumtypid
                                WHERE t.typname = 'ownerrole' AND e.enumlabel = 'USER'
                            ) THEN
                                ALTER TYPE ownerrole RENAME VALUE 'user' TO 'USER';
                            END IF;

                            ALTER TYPE ownerrole ADD VALUE IF NOT EXISTS 'ADMINISTRATOR';
                            ALTER TYPE ownerrole ADD VALUE IF NOT EXISTS 'USER';
                        END IF;
                    END$$;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    ALTER TABLE owners
                    ADD COLUMN IF NOT EXISTS role ownerrole;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    ALTER TABLE owners
                    ADD COLUMN IF NOT EXISTS account_owner_id INTEGER;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE owners
                    SET role = 'ADMINISTRATOR'::ownerrole
                    WHERE role IS NULL;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE owners
                    SET account_owner_id = id
                    WHERE account_owner_id IS NULL;
                    """
                )
            )
            conn.execute(text("ALTER TABLE owners ALTER COLUMN role SET NOT NULL;"))
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1
                            FROM pg_constraint
                            WHERE conname = 'fk_owners_account_owner'
                        ) THEN
                            ALTER TABLE owners
                            ADD CONSTRAINT fk_owners_account_owner
                            FOREIGN KEY (account_owner_id) REFERENCES owners(id) ON DELETE SET NULL;
                        END IF;
                    END$$;
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_owner_account_owner_id ON owners (account_owner_id);"))

            # Allow duplicate zone_id values across different owners.
            conn.execute(text("ALTER TABLE zones DROP CONSTRAINT IF EXISTS zones_zone_id_key;"))
            conn.execute(text("DROP INDEX IF EXISTS zones_zone_id_key;"))
            conn.execute(text("DROP INDEX IF EXISTS ix_zones_zone_id;"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_zones_zone_id ON zones (zone_id);"))
            conn.execute(text("ALTER TABLE zones ADD COLUMN IF NOT EXISTS creator_id INTEGER;"))
            conn.execute(
                text(
                    """
                    UPDATE zones
                    SET creator_id = owner_id
                    WHERE creator_id IS NULL;
                    """
                )
            )
            conn.execute(text("ALTER TABLE zones ALTER COLUMN creator_id SET NOT NULL;"))
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1
                            FROM pg_constraint
                            WHERE conname = 'fk_zones_creator_owner'
                        ) THEN
                            ALTER TABLE zones
                            ADD CONSTRAINT fk_zones_creator_owner
                            FOREIGN KEY (creator_id) REFERENCES owners(id) ON DELETE CASCADE;
                        END IF;
                    END$$;
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_zone_creator_id ON zones (creator_id);"))
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'contractmessagetype') THEN
                            ALTER TYPE contractmessagetype ADD VALUE IF NOT EXISTS 'UNKNOWN';
                            ALTER TYPE contractmessagetype ADD VALUE IF NOT EXISTS 'PRIVATE';
                            ALTER TYPE contractmessagetype ADD VALUE IF NOT EXISTS 'PA';
                            ALTER TYPE contractmessagetype ADD VALUE IF NOT EXISTS 'SERVICE';
                            ALTER TYPE contractmessagetype ADD VALUE IF NOT EXISTS 'WELLNESS_CHECK';
                            ALTER TYPE contractmessagetype ADD VALUE IF NOT EXISTS 'PERMISSION';
                            ALTER TYPE contractmessagetype ADD VALUE IF NOT EXISTS 'CHAT';
                        END IF;
                    END$$;
                    """
                )
            )

            # Backward-compatible schema patch for older deployments missing member location fields.
            conn.execute(
                text(
                    """
                    ALTER TABLE member_locations
                    ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    ALTER TABLE member_locations
                    ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    ALTER TABLE member_locations
                    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();
                    """
                )
            )


def drop_db():
    """Drop all database tables."""
    Base.metadata.drop_all(bind=engine)
