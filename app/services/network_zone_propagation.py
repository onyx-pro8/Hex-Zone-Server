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


def network_zone_ids_containing_point(
    db: Session,
    *,
    latitude: float,
    longitude: float,
) -> list[str]:
    """Distinct network ids (``zones.zone_id``) for acceptable zones containing a point."""
    zone_record_ids = evaluate_zone_records_containing_point(db, float(latitude), float(longitude))
    zone_rows = _zone_rows_for_records(db, zone_record_ids)
    return sorted({(z.zone_id or "").strip() for z in zone_rows if (z.zone_id or "").strip()})


def network_owner_ids_for_unknown_fanout(
    db: Session,
    sender: Owner,
    *,
    origin_lat: float,
    origin_lon: float,
    exclude_owner_id: int | None = None,
) -> tuple[list[int], list[str]]:
    """Candidate owner ids for UNKNOWN: same-network pool from membership or host geometry."""
    if owner_participates_in_network(db, sender):
        network_ids = [(sender.zone_id or "").strip()]
    else:
        network_ids = network_zone_ids_containing_point(
            db,
            latitude=float(origin_lat),
            longitude=float(origin_lon),
        )
    pool: set[int] = set()
    for network_id in network_ids:
        if not network_id:
            continue
        pool.update(
            network_owner_ids_with_coordinates(
                db,
                network_id,
                exclude_owner_id=None,
            )
        )
    if exclude_owner_id is not None:
        pool.discard(int(exclude_owner_id))
    return sorted(pool), [nid for nid in network_ids if nid]


def _empty_propagation_meta(*, network_zone_id: str = "") -> dict:
    return {
        "strategy": "network_no_acceptable_zone",
        "network_zone_id": network_zone_id,
        "sender_zone_record_ids": [],
        "sender_zone_ids": [],
        "recipient_owner_ids": [],
        "primary_zone_record_ids": [],
        "secondary_zone_record_ids": [],
        "matched_network_zone_ids": [],
    }


def _recipients_for_network_zone_rows(
    db: Session,
    *,
    network_id: str,
    network_rows: list[Zone],
    sender: Owner,
    exclude_owner_id: int | None,
) -> tuple[list[str], list[int], list[int], dict] | None:
    if not network_rows:
        return None

    admin = resolve_network_administrator(db, network_id)
    admin_id = int(admin.id) if admin is not None else None

    primary_rows = [z for z in network_rows if _is_primary_zone_row(z, network_admin_id=admin_id)]
    secondary_rows = [z for z in network_rows if not _is_primary_zone_row(z, network_admin_id=admin_id)]

    if not primary_rows and not secondary_rows:
        return None

    matched_record_ids = [int(z.id) for z in (primary_rows or secondary_rows)]
    zone_ids = zone_ids_for_zone_records(db, matched_record_ids)

    if primary_rows:
        account_root = admin if admin is not None else sender
        recipient_ids = set(account_propagation_owner_ids(db, account_root))
        strategy = "primary_zone_network_members"
    else:
        recipient_ids = {int(z.creator_id) for z in secondary_rows}
        strategy = "secondary_zone_creator_only"

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
        "matched_network_zone_ids": [network_id],
    }
    return zone_ids, matched_record_ids, sorted_recipients, meta


def _merge_propagation_results(
    results: list[tuple[list[str], list[int], list[int], dict]],
) -> tuple[list[str], list[int], list[int], dict]:
    all_zone_ids: list[str] = []
    all_record_ids: list[int] = []
    all_recipients: set[int] = set()
    all_primary: list[int] = []
    all_secondary: list[int] = []
    network_ids: list[str] = []
    strategy = "network_no_acceptable_zone"
    admin_id: int | None = None

    for zone_ids, record_ids, recipients, meta in results:
        for zid in zone_ids:
            if zid not in all_zone_ids:
                all_zone_ids.append(zid)
        for rid in record_ids:
            if rid not in all_record_ids:
                all_record_ids.append(rid)
        all_recipients.update(recipients)
        for rid in meta.get("primary_zone_record_ids") or []:
            if rid not in all_primary:
                all_primary.append(int(rid))
        for rid in meta.get("secondary_zone_record_ids") or []:
            if rid not in all_secondary:
                all_secondary.append(int(rid))
        for nid in meta.get("matched_network_zone_ids") or []:
            if nid not in network_ids:
                network_ids.append(nid)
        if meta.get("strategy") == "primary_zone_network_members":
            strategy = "primary_zone_network_members"
        elif strategy != "primary_zone_network_members" and meta.get("strategy"):
            strategy = str(meta["strategy"])
        if meta.get("network_administrator_id") is not None:
            admin_id = int(meta["network_administrator_id"])

    merged_meta = {
        "strategy": strategy,
        "network_zone_id": network_ids[0] if network_ids else "",
        "network_administrator_id": admin_id,
        "sender_zone_ids": all_zone_ids,
        "sender_zone_record_ids": all_record_ids,
        "primary_zone_record_ids": all_primary,
        "secondary_zone_record_ids": all_secondary,
        "recipient_owner_ids": sorted(all_recipients),
        "matched_network_zone_ids": network_ids,
    }
    return all_zone_ids, all_record_ids, sorted(all_recipients), merged_meta


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

  When ``network_zone_id`` is omitted, networks are inferred from acceptable-zone
  geometry at the sender's coordinates. This allows **non-invited** owners (not on
  the network account) who are physically inside another network's primary zone to
  reach that network's administrator and invited members.
    """
    zone_record_ids = evaluate_zone_records_containing_point(db, float(latitude), float(longitude))
    zone_rows = _zone_rows_for_records(db, zone_record_ids)

    if network_zone_id is not None:
        network_id = (network_zone_id or "").strip()
        network_rows = [z for z in zone_rows if (z.zone_id or "").strip() == network_id]
        result = _recipients_for_network_zone_rows(
            db,
            network_id=network_id,
            network_rows=network_rows,
            sender=sender,
            exclude_owner_id=exclude_owner_id,
        )
        if result is None:
            return [], [], [], _empty_propagation_meta(network_zone_id=network_id)
        return result

    if not zone_rows:
        return [], [], [], _empty_propagation_meta(network_zone_id=(sender.zone_id or "").strip())

    by_network: dict[str, list[Zone]] = {}
    for zone_row in zone_rows:
        nid = (zone_row.zone_id or "").strip()
        if nid:
            by_network.setdefault(nid, []).append(zone_row)

    primary_matches: list[tuple[str, list[Zone]]] = []
    secondary_matches: list[tuple[str, list[Zone]]] = []

    for network_id, network_rows in by_network.items():
        admin = resolve_network_administrator(db, network_id)
        admin_id = int(admin.id) if admin is not None else None
        primary_rows = [z for z in network_rows if _is_primary_zone_row(z, network_admin_id=admin_id)]
        secondary_rows = [z for z in network_rows if not _is_primary_zone_row(z, network_admin_id=admin_id)]
        if primary_rows:
            primary_matches.append((network_id, network_rows))
        elif secondary_rows:
            secondary_matches.append((network_id, network_rows))

    chosen = primary_matches or secondary_matches
    partial_results: list[tuple[list[str], list[int], list[int], dict]] = []
    for network_id, network_rows in chosen:
        partial = _recipients_for_network_zone_rows(
            db,
            network_id=network_id,
            network_rows=network_rows,
            sender=sender,
            exclude_owner_id=None,
        )
        if partial is not None:
            partial_results.append(partial)

    if not partial_results:
        return [], [], [], _empty_propagation_meta(network_zone_id=(sender.zone_id or "").strip())

    zone_ids, record_ids, recipients, meta = _merge_propagation_results(partial_results)
    if exclude_owner_id is not None:
        recipients = [oid for oid in recipients if oid != int(exclude_owner_id)]
        meta = {**meta, "recipient_owner_ids": recipients}
    return zone_ids, record_ids, recipients, meta


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
