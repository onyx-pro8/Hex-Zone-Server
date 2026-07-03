"""Primary vs secondary acceptable-zone message routing for a network."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Owner, Zone
from app.models.owner import OwnerRole
from app.services.access_policy import account_propagation_owner_ids
from app.services.geospatial_service import (
    evaluate_zone_records_containing_point,
    zone_ids_for_zone_records,
)


def resolve_network_administrator(db: Session, network_zone_id: str) -> Owner | None:
    """Network administrator: active **ADMINISTRATOR** with this network id (``owners.zone_id``)."""
    zid = (network_zone_id or "").strip()
    if not zid:
        return None
    return (
        db.query(Owner)
        .filter(
            Owner.zone_id == zid,
            Owner.role == OwnerRole.ADMINISTRATOR,
            Owner.active.is_(True),
        )
        .order_by(Owner.id.asc())
        .first()
    )


def _zone_rows_for_records(db: Session, zone_record_ids: list[int]) -> list[Zone]:
    if not zone_record_ids:
        return []
    return (
        db.query(Zone)
        .filter(Zone.id.in_(zone_record_ids), Zone.active.is_(True))
        .all()
    )


def _is_primary_zone_row(zone: Zone, *, network_admin_id: int | None) -> bool:
    """Primary acceptable zone = geometry created by the network administrator."""
    if network_admin_id is None:
        return False
    return int(zone.creator_id) == int(network_admin_id)


def owner_participates_in_network(db: Session, owner: Owner) -> bool:
    """True when the owner is the network administrator or an invited member."""
    network_id = (owner.zone_id or "").strip()
    if not network_id:
        return False
    admin = resolve_network_administrator(db, network_id)
    if admin is None:
        return False
    if owner.role == OwnerRole.ADMINISTRATOR:
        if not bool(owner.active):
            return False
        return (owner.zone_id or "").strip() == network_id
    root_id = owner.account_owner_id
    if root_id is None:
        return False
    root = db.get(Owner, int(root_id))
    if root is None or not bool(root.active):
        return False
    return (root.zone_id or "").strip() == network_id


def resolve_network_geo_propagation_recipients(
    db: Session,
    sender: Owner,
    *,
    latitude: float,
    longitude: float,
    exclude_owner_id: int | None = None,
    network_zone_id: str | None = None,
) -> tuple[list[str], list[int], list[int], dict]:
    """Resolve geo-propagation recipients using primary vs secondary zone rules.

    - Inside **primary** (admin-created) acceptable zone → administrator + all
      invited members on the same account (``account_propagation_owner_ids``).
    - Inside **secondary** (member-created) acceptable zone only → each matched
      secondary zone's **creator** only.
    - Outside primary and not inside any secondary acceptable zone → nobody.

    Only zone rows whose ``zone_id`` matches the network id are considered. Pass
    ``network_zone_id`` when the routing owner is a proxy (e.g. network-access guest).
    """
    network_id = (network_zone_id or sender.zone_id or "").strip()
    zone_record_ids = evaluate_zone_records_containing_point(db, float(latitude), float(longitude))
    zone_rows = _zone_rows_for_records(db, zone_record_ids)
    network_rows = [z for z in zone_rows if (z.zone_id or "").strip() == network_id]

    empty_meta = {
        "strategy": "network_no_acceptable_zone",
        "network_zone_id": network_id,
        "sender_zone_record_ids": [],
        "sender_zone_ids": [],
        "recipient_owner_ids": [],
        "primary_zone_record_ids": [],
        "secondary_zone_record_ids": [],
    }
    if not network_rows:
        return [], [], [], empty_meta

    admin = resolve_network_administrator(db, network_id)
    admin_id = int(admin.id) if admin is not None else None

    primary_rows = [z for z in network_rows if _is_primary_zone_row(z, network_admin_id=admin_id)]
    secondary_rows = [z for z in network_rows if not _is_primary_zone_row(z, network_admin_id=admin_id)]

    matched_record_ids = [int(z.id) for z in (primary_rows or secondary_rows)]
    zone_ids = zone_ids_for_zone_records(db, matched_record_ids)

    if primary_rows:
        account_root = admin if admin is not None else sender
        recipient_ids = set(account_propagation_owner_ids(db, account_root))
        strategy = "primary_zone_network_members"
    elif secondary_rows:
        recipient_ids = {int(z.creator_id) for z in secondary_rows}
        strategy = "secondary_zone_creator_only"
    else:
        return [], [], [], empty_meta

    if exclude_owner_id is not None:
        recipient_ids.discard(int(exclude_owner_id))

    sorted_recipients = sorted(recipient_ids)
    meta = {
        "strategy": strategy,
        "network_zone_id": network_id,
        "network_administrator_id": admin_id,
        "sender_zone_ids": zone_ids,
        "sender_zone_record_ids": matched_record_ids,
        "primary_zone_record_ids": [int(z.id) for z in primary_rows],
        "secondary_zone_record_ids": [int(z.id) for z in secondary_rows],
        "recipient_owner_ids": sorted_recipients,
    }
    return zone_ids, matched_record_ids, sorted_recipients, meta


def network_owner_ids_with_coordinates(
    db: Session,
    network_zone_id: str,
    *,
    exclude_owner_id: int | None = None,
) -> list[int]:
    """Active owners on the network id who have stored coordinates (UNKNOWN fan-out pool)."""
    zid = (network_zone_id or "").strip()
    if not zid:
        return []
    rows = (
        db.query(Owner.id)
        .filter(
            Owner.zone_id == zid,
            Owner.active.is_(True),
            Owner.latitude.isnot(None),
            Owner.longitude.isnot(None),
        )
        .all()
    )
    out = sorted({int(row[0]) for row in rows})
    if exclude_owner_id is not None:
        out = [oid for oid in out if oid != int(exclude_owner_id)]
    return out
