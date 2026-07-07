"""Member listing and location tracking services."""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Device, MemberLocation, Owner, PushToken, Zone
from app.services.access_policy import messaging_visible_owner_ids
from app.services.geospatial_service import evaluate_member_zones
from app.services.zone_membership_service import refresh_owner_memberships


def set_member_live_position(
    db: Session, owner_id: int, latitude: float, longitude: float
) -> None:
    """Persist live GPS on `member_locations` without zone membership refresh."""
    now = datetime.utcnow()
    row = db.get(MemberLocation, owner_id)
    if row is None:
        row = MemberLocation(owner_id=owner_id, latitude=latitude, longitude=longitude)
        db.add(row)
    else:
        row.latitude = latitude
        row.longitude = longitude
        row.updated_at = now
    db.flush()


def upsert_member_location(db: Session, owner_id: int, latitude: float, longitude: float) -> dict:
    now = datetime.utcnow()
    row = db.get(MemberLocation, owner_id)
    if row is None:
        row = MemberLocation(owner_id=owner_id, latitude=latitude, longitude=longitude)
        db.add(row)
    else:
        row.latitude = latitude
        row.longitude = longitude
        row.updated_at = now
    # Live GPS stays on `member_locations` only. `owners.latitude/longitude` hold
    # the geocoded registered home address (SENSOR / WELLNESS_CHECK routing).
    owner = db.get(Owner, owner_id)
    db.flush()
    zone_ids = evaluate_member_zones(db, latitude, longitude, [owner_id])
    if owner:
        zone_ids = refresh_owner_memberships(db, owner, latitude, longitude)
    return {"latitude": row.latitude, "longitude": row.longitude, "zones": zone_ids}


def get_owner_live_coordinates(db: Session, owner_id: int) -> tuple[float, float] | None:
    """Return the owner's latest live GPS fix from `member_locations`, if any."""
    row = db.get(MemberLocation, owner_id)
    if row is None:
        return None
    return float(row.latitude), float(row.longitude)


def list_members(db: Session, owner: Owner, active: bool | None = None) -> list[dict]:
    owner_ids = messaging_visible_owner_ids(db, owner, include_inactive=True)
    query = db.query(Owner).filter(Owner.id.in_(owner_ids))
    if active is not None:
        query = query.filter(Owner.active.is_(active))
    members = query.all()
    output: list[dict] = []
    for member in members:
        location = db.get(MemberLocation, member.id)
        if location is None:
            location = (
                db.query(Device)
                .filter(
                    Device.owner_id == member.id,
                    Device.latitude.isnot(None),
                    Device.longitude.isnot(None),
                )
                .order_by(Device.updated_at.desc())
                .first()
            )
        zones = db.query(Zone.zone_id).filter(Zone.owner_id == member.id).all()
        output.append(
            {
                "id": str(member.id),
                "name": f"{member.first_name} {member.last_name}".strip(),
                "first_name": member.first_name,
                "last_name": member.last_name,
                "email": member.email,
                "account_owner_id": member.account_owner_id or member.id,
                "role": member.role.value,
                "account_type": member.account_type.value,
                "address": member.address,
                "zone_id": member.zone_id,
                "active": member.active,
                "location": None
                if not location
                else {"latitude": location.latitude, "longitude": location.longitude},
                "lastSeen": None if not location else location.updated_at.isoformat(),
                "zones": [row[0] for row in zones],
            }
        )
    return output


def upsert_push_token(db: Session, owner_id: int, token: str, platform: str) -> dict:
    row = db.query(PushToken).filter(PushToken.token == token).first()
    if row is None:
        row = PushToken(owner_id=owner_id, token=token, platform=platform.upper(), active=True)
        db.add(row)
    else:
        row.owner_id = owner_id
        row.platform = platform.upper()
        row.active = True
    db.flush()
    return {"token": row.token, "platform": row.platform, "active": row.active}
