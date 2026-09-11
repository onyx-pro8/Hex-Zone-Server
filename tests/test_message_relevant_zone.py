"""Relevant-zone inbox heading rules."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.services.message_relevant_zone_service import (
    _compose_heading_label,
    _multi_zone_sender_label,
    attach_relevant_zone_metadata,
    build_recipient_zone_record_ids,
    resolve_relevant_zone_for_viewer,
)


@pytest.fixture()
def rz_db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()


def test_compose_heading_uses_sender_network_not_zone_network():
    assert (
        _compose_heading_label(
            zone_name="Downtown Grid",
            sender_network_id="ZN-HOME",
        )
        == "Downtown Grid (ZN-HOME)"
    )


def test_multi_zone_sender_label():
    assert _multi_zone_sender_label(4) == "My zone and 3 more zones"
    assert _multi_zone_sender_label(2) == "My zone and 1 more zones"


def test_build_recipient_zone_record_ids_defaults_to_primary():
    mapping = build_recipient_zone_record_ids(
        recipient_owner_ids=[5, 6],
        zone_meta={
            "primary_zone_record_ids": [101],
            "sender_zone_record_ids": [101, 202],
        },
    )
    assert mapping == {"5": 101, "6": 101}


def test_attach_and_resolve_foreign_zone_keeps_sender_network(rz_db, monkeypatch):
    def fake_load(_db, record_ids, *, sender_network_id=None):
        return {
            501: {
                "zone_record_id": 501,
                "name": "Other district zone",
                "network_id": "ZN-OTHER",
                "sender_network_id": sender_network_id,
                "label": (
                    f"Other district zone ({sender_network_id})"
                    if sender_network_id
                    else "Other district zone (ZN-OTHER)"
                ),
            }
        }

    monkeypatch.setattr(
        "app.services.message_relevant_zone_service._load_zone_display_rows",
        fake_load,
    )

    metadata: dict = {}
    zone_meta = {
        "primary_zone_record_ids": [501],
        "sender_zone_record_ids": [501],
        "recipient_zone_record_ids": {"5": 501},
    }
    attach_relevant_zone_metadata(
        rz_db,
        metadata=metadata,
        zone_meta=zone_meta,
        delivered_owner_ids=[5],
        sender_network_id="ZN-HOME",
    )

    assert metadata["sender_network_id"] == "ZN-HOME"
    assert metadata["recipient_relevant_zones"]["5"]["label"] == "Other district zone (ZN-HOME)"

    fields = resolve_relevant_zone_for_viewer(
        rz_db,
        metadata=metadata,
        viewer_owner_id=5,
        sender_id=2,
    )
    assert fields["relevant_zone_name"] == "Other district zone"
    assert fields["relevant_zone_network_id"] == "ZN-HOME"
    assert fields["relevant_zone_label"] == "Other district zone (ZN-HOME)"


def test_resolve_multi_zone_summary_for_sender(rz_db):
    metadata = {
        "sender_network_id": "ZN-HOME",
        "sender_matched_zone_count": 4,
        "sender_relevant_zone": {
            "name": "Primary",
            "network_id": "ZN-HOME",
            "sender_network_id": "ZN-HOME",
            "label": "Primary (ZN-HOME)",
        },
        "sender_relevant_zone_record_id": 101,
    }
    fields = resolve_relevant_zone_for_viewer(
        rz_db,
        metadata=metadata,
        viewer_owner_id=2,
        sender_id=2,
    )
    assert fields["relevant_zone_label"] == "My zone and 3 more zones"
    assert fields["relevant_zone_network_id"] == "ZN-HOME"
