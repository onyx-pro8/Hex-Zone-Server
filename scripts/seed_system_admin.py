"""Create the default system administrator if missing. Run from server/: python scripts/seed_system_admin.py"""
from app.database import session_maker, init_db
from app.services.system_admin_seed import (
    SYSTEM_ADMIN_EMAIL,
    SYSTEM_ADMIN_PASSWORD,
    ensure_system_admin,
)


def main() -> None:
    init_db()
    with session_maker() as db:
        owner = ensure_system_admin(db)
        if owner:
            print(f"System admin ready: {SYSTEM_ADMIN_EMAIL} / {SYSTEM_ADMIN_PASSWORD}")
            print(f"  account_type={owner.account_type.value}")
            print(f"  zone_id={owner.zone_id}")
            print(f"  role={owner.role.value}")


if __name__ == "__main__":
    main()
