"""Resolve the acceptable zone relevant to a message viewer.

Heading rules:
- Normal: ``{delivery zone name} ({sender network id})``
  Even when the sender is inside another network's zone, the parentheses always
  use the sender's home/account network id.
- Sender with multiple matched zones: ``My zone and N more zones``.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Owner, Zone


def _pick_sender_zone_record_id(zone_meta: dict) -> int | None:
    primary = zone_meta.get("primary_zone_record_ids") or []
    if primary:
        return int(primary[0])
    sender_records = zone_meta.get("sender_zone_record_ids") or []
    if sender_records:
        return int(sender_records[0])
    return None


def _unique_int_ids(raw: object) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for value in raw:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed not in out:
            out.append(parsed)
    return out


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


def _compose_heading_label(*, zone_name: str | None, sender_network_id: str | None) -> str | None:
    clean_name = (zone_name or "").strip()
    clean_sender_net = (sender_network_id or "").strip()
    if clean_name and clean_sender_net:
        return f"{clean_name} ({clean_sender_net})"
    return clean_name or clean_sender_net or None


def _multi_zone_sender_label(matched_count: int) -> str:
    more = max(int(matched_count) - 1, 0)
    return f"My zone and {more} more zones"


def _zone_display_payload(
    *,
    zone_record_id: int,
    name: str | None,
    zone_network_id: str | None,
    sender_network_id: str | None,
) -> dict:
    clean_name = (name or "").strip()
    clean_zone_net = (zone_network_id or "").strip()
    clean_sender_net = (sender_network_id or "").strip()
    label = _compose_heading_label(
        zone_name=clean_name,
        sender_network_id=clean_sender_net or clean_zone_net or None,
    )
    return {
        "zone_record_id": int(zone_record_id),
        "name": clean_name or None,
        "network_id": clean_zone_net or None,
        "sender_network_id": clean_sender_net or None,
        "label": label,
    }


def _load_zone_display_rows(
    db: Session,
    record_ids: set[int],
    *,
    sender_network_id: str | None = None,
) -> dict[int, dict]:
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
            zone_network_id=row.zone_id,
            sender_network_id=sender_network_id,
        )
        for row in rows
    }


def _sender_matched_zone_count(meta: dict, zone_meta: dict | None = None) -> int:
    stored = meta.get("sender_matched_zone_count")
    if isinstance(stored, int) and stored > 0:
        return int(stored)
    fanout = meta.get("fanout") if isinstance(meta.get("fanout"), dict) else {}
    source = zone_meta if isinstance(zone_meta, dict) else fanout
    ids = _unique_int_ids(source.get("sender_zone_record_ids"))
    if ids:
        return len(ids)
    primary = _unique_int_ids(source.get("primary_zone_record_ids"))
    secondary = _unique_int_ids(source.get("secondary_zone_record_ids"))
    merged = _unique_int_ids([*primary, *secondary])
    return len(merged)


def _sender_network_id_from_meta(
    db: Session,
    meta: dict,
    sender_id: int | None,
) -> str | None:
    raw = meta.get("sender_network_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    embedded = meta.get("sender_relevant_zone")
    if isinstance(embedded, dict):
        nested = embedded.get("sender_network_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    if sender_id is not None:
        owner = db.get(Owner, int(sender_id))
        if owner is not None and (owner.zone_id or "").strip():
            return str(owner.zone_id).strip()
    return None


def attach_relevant_zone_metadata(
    db: Session,
    *,
    metadata: dict,
    zone_meta: dict,
    delivered_owner_ids: list[int],
    sender_network_id: str | None = None,
) -> None:
    """Mutates ``metadata`` with per-recipient and sender relevant zone display."""
    clean_sender_net = (sender_network_id or "").strip() or None
    if clean_sender_net:
        metadata["sender_network_id"] = clean_sender_net

    matched_count = _sender_matched_zone_count(metadata, zone_meta)
    if matched_count > 0:
        metadata["sender_matched_zone_count"] = matched_count

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

    zones = _load_zone_display_rows(
        db,
        record_ids,
        sender_network_id=clean_sender_net,
    )
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
    sender_network_id = _sender_network_id_from_meta(db, meta, sender_id)

    if sender_id is not None and int(viewer_owner_id) == int(sender_id):
        matched_count = _sender_matched_zone_count(meta)
        if matched_count > 1:
            return {
                "relevant_zone_name": "My zone",
                "relevant_zone_network_id": sender_network_id,
                "relevant_zone_label": _multi_zone_sender_label(matched_count),
            }

    embedded = _embedded_zone_for_viewer(meta, viewer_owner_id, sender_id, sender_network_id)
    if embedded:
        return embedded

    record_id = _resolve_zone_record_id(meta, viewer_owner_id, sender_id, db)
    if record_id is None:
        return {
            "relevant_zone_name": None,
            "relevant_zone_network_id": sender_network_id,
            "relevant_zone_label": None,
        }
    payload = _load_zone_display_rows(
        db,
        {int(record_id)},
        sender_network_id=sender_network_id,
    ).get(int(record_id))
    if payload is None:
        return {
            "relevant_zone_name": None,
            "relevant_zone_network_id": sender_network_id,
            "relevant_zone_label": None,
        }
    return {
        "relevant_zone_name": payload.get("name"),
        "relevant_zone_network_id": sender_network_id or payload.get("sender_network_id") or payload.get("network_id"),
        "relevant_zone_label": payload.get("label"),
    }


def _embedded_zone_for_viewer(
    meta: dict,
    viewer_owner_id: int,
    sender_id: int | None,
    sender_network_id: str | None,
) -> dict[str, str | int | None] | None:
    if sender_id is not None and int(viewer_owner_id) == int(sender_id):
        raw = meta.get("sender_relevant_zone")
        if isinstance(raw, dict):
            return _display_from_embedded(raw, sender_network_id)
    recipient_zones = meta.get("recipient_relevant_zones")
    if isinstance(recipient_zones, dict):
        raw = recipient_zones.get(str(viewer_owner_id))
        if isinstance(raw, dict):
            return _display_from_embedded(raw, sender_network_id)
    return None


def _display_from_embedded(
    raw: dict,
    sender_network_id: str | None,
) -> dict[str, str | int | None]:
    name = raw.get("name") if isinstance(raw.get("name"), str) else None
    zone_network = raw.get("network_id") if isinstance(raw.get("network_id"), str) else None
    embedded_sender_net = (
        raw.get("sender_network_id") if isinstance(raw.get("sender_network_id"), str) else None
    )
    resolved_sender_net = (
        (sender_network_id or "").strip()
        or (embedded_sender_net or "").strip()
        or (zone_network or "").strip()
        or None
    )
    label = raw.get("label") if isinstance(raw.get("label"), str) else None
    # Recompose when we know the sender network so foreign-zone delivery still
    # shows the sender's network id in parentheses.
    recomposed = _compose_heading_label(zone_name=name, sender_network_id=resolved_sender_net)
    return {
        "relevant_zone_name": name,
        "relevant_zone_network_id": resolved_sender_net,
        "relevant_zone_label": recomposed or label,
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
