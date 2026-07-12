"""Resolve the acceptable zone relevant to a message viewer."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Zone


def _pick_sender_zone_record_id(zone_meta: dict) -> int | None:
    primary = zone_meta.get("primary_zone_record_ids") or []
    if primary:
        return int(primary[0])
    sender_records = zone_meta.get("sender_zone_record_ids") or []
    if sender_records:
        return int(sender_records[0])
    return None


def build_recipient_zone_record_ids(
    *,
    recipient_owner_ids: list[int],
    zone_meta: dict,
) -> dict[str, int]:
    """Ensure every delivered recipient maps to the zone that explains delivery."""
    existing_raw = zone_meta.get("recipient_zone_record_ids")
    mapping: dict[str, int] = {}
    if isinstance(existing_raw, dict):
        for key, value in existing_raw.items():
            if isinstance(value, int) and not isinstance(value, bool):
                mapping[str(key)] = int(value)
            elif isinstance(value, (float, str)) and str(value).strip().isdigit():
                mapping[str(key)] = int(value)

    default_record_id = _pick_sender_zone_record_id(zone_meta)
    if default_record_id is None:
        secondary_ids = zone_meta.get("secondary_zone_record_ids") or []
        if len(secondary_ids) == 1:
            default_record_id = int(secondary_ids[0])

    if default_record_id is not None:
        for owner_id in recipient_owner_ids:
            key = str(int(owner_id))
            mapping.setdefault(key, default_record_id)

    return mapping


def _zone_display_payload(*, zone_record_id: int, name: str | None, network_id: str | None) -> dict:
    clean_name = (name or "").strip()
    clean_network = (network_id or "").strip()
    label = (
        f"{clean_name} ({clean_network})"
        if clean_name and clean_network
        else clean_name or clean_network or None
    )
    return {
        "zone_record_id": int(zone_record_id),
        "name": clean_name or None,
        "network_id": clean_network or None,
        "label": label,
    }


def _load_zone_display_rows(db: Session, record_ids: set[int]) -> dict[int, dict]:
    if not record_ids:
        return {}
    rows = (
        db.query(Zone.id, Zone.name, Zone.zone_id)
        .filter(Zone.id.in_(tuple(record_ids)))
        .all()
    )
    return {
        int(row.id): _zone_display_payload(
            zone_record_id=int(row.id),
            name=row.name,
            network_id=row.zone_id,
        )
        for row in rows
    }


def attach_relevant_zone_metadata(
    db: Session,
    *,
    metadata: dict,
    zone_meta: dict,
    delivered_owner_ids: list[int],
) -> None:
    """Mutates ``metadata`` with per-recipient and sender relevant zone display."""
    recipient_map = build_recipient_zone_record_ids(
        recipient_owner_ids=delivered_owner_ids,
        zone_meta=zone_meta,
    )
    metadata["recipient_zone_record_ids"] = recipient_map

    sender_record_id = _pick_sender_zone_record_id(zone_meta)
    if sender_record_id is not None:
        metadata["sender_relevant_zone_record_id"] = sender_record_id

    record_ids = set(recipient_map.values())
    if sender_record_id is not None:
        record_ids.add(sender_record_id)

    if not record_ids:
        return

    zones = _load_zone_display_rows(db, record_ids)
    recipient_zones = {
        owner_key: payload
        for owner_key, record_id in recipient_map.items()
        if (payload := zones.get(int(record_id))) is not None and payload.get("label")
    }
    if recipient_zones:
        metadata["recipient_relevant_zones"] = recipient_zones
    if sender_record_id is not None and (sender_payload := zones.get(int(sender_record_id))):
        if sender_payload.get("label"):
            metadata["sender_relevant_zone"] = sender_payload


def resolve_relevant_zone_for_viewer(
    db: Session,
    *,
    metadata: dict,
    viewer_owner_id: int,
    sender_id: int | None,
) -> dict[str, str | int | None]:
    """Viewer-specific acceptable zone label for inbox rows."""
    meta = metadata if isinstance(metadata, dict) else {}
    embedded = _embedded_zone_for_viewer(meta, viewer_owner_id, sender_id)
    if embedded:
        return embedded

    record_id = _resolve_zone_record_id(meta, viewer_owner_id, sender_id, db)
    if record_id is None:
        return {
            "relevant_zone_name": None,
            "relevant_zone_network_id": None,
            "relevant_zone_label": None,
        }
    payload = _load_zone_display_rows(db, {int(record_id)}).get(int(record_id))
    if payload is None:
        return {
            "relevant_zone_name": None,
            "relevant_zone_network_id": None,
            "relevant_zone_label": None,
        }
    return {
        "relevant_zone_name": payload.get("name"),
        "relevant_zone_network_id": payload.get("network_id"),
        "relevant_zone_label": payload.get("label"),
    }


def _embedded_zone_for_viewer(
    meta: dict,
    viewer_owner_id: int,
    sender_id: int | None,
) -> dict[str, str | int | None] | None:
    if sender_id is not None and int(viewer_owner_id) == int(sender_id):
        raw = meta.get("sender_relevant_zone")
        if isinstance(raw, dict):
            return _display_from_embedded(raw)
    recipient_zones = meta.get("recipient_relevant_zones")
    if isinstance(recipient_zones, dict):
        raw = recipient_zones.get(str(viewer_owner_id))
        if isinstance(raw, dict):
            return _display_from_embedded(raw)
    return None


def _display_from_embedded(raw: dict) -> dict[str, str | int | None]:
    name = raw.get("name")
    network_id = raw.get("network_id")
    label = raw.get("label")
    return {
        "relevant_zone_name": name if isinstance(name, str) else None,
        "relevant_zone_network_id": network_id if isinstance(network_id, str) else None,
        "relevant_zone_label": label if isinstance(label, str) else None,
    }


def _resolve_zone_record_id(
    meta: dict,
    viewer_owner_id: int,
    sender_id: int | None,
    db: Session,
) -> int | None:
    mapping = meta.get("recipient_zone_record_ids")
    if isinstance(mapping, dict):
        raw = mapping.get(str(viewer_owner_id))
        if raw is not None:
            return int(raw)

    if sender_id is not None and int(viewer_owner_id) == int(sender_id):
        raw = meta.get("sender_relevant_zone_record_id")
        if raw is not None:
            return int(raw)

    fanout = meta.get("fanout") if isinstance(meta.get("fanout"), dict) else meta
    strategy = fanout.get("strategy")
    if strategy in {
        "primary_zone_network_members",
        "primary_zone_gps_fanout",
        "private_plus_network_shared",
    }:
        primary = fanout.get("primary_zone_record_ids") or []
        if primary:
            return int(primary[0])
        sender_records = fanout.get("sender_zone_record_ids") or []
        if sender_records:
            return int(sender_records[0])

    if strategy == "secondary_zone_creator_only":
        secondary_ids = fanout.get("secondary_zone_record_ids") or []
        if secondary_ids:
            from app.models import Zone as ZoneModel

            rows = (
                db.query(ZoneModel.id)
                .filter(
                    ZoneModel.id.in_([int(x) for x in secondary_ids]),
                    ZoneModel.creator_id == int(viewer_owner_id),
                )
                .all()
            )
            if rows:
                return int(rows[0][0])
    return None
