"""Unit tests for **app.domain.event_id** (guest access matching)."""

from app.domain.event_id import (
    canonical_event_id,
    event_id_lowercase_sql_in_values,
    event_ids_equivalent,
)
from app.services.guest_access_qr_token_service import merge_event_id_for_arrival


def test_canonical_evt_numeric_variants():
    assert canonical_event_id("EVT-1234") == "1234"
    assert canonical_event_id("evt_1234") == "1234"
    assert canonical_event_id("Evt1234") == "1234"
    assert canonical_event_id("  1234  ") == "1234"
    assert canonical_event_id("0012") == "0012"
    assert canonical_event_id("EVT-0012") == "0012"


def test_canonical_non_evt_casefold():
    assert canonical_event_id("DELIVERY-42") == "delivery-42"
    assert canonical_event_id("Summer-Gala") == "summer-gala"


def test_canonical_evt_with_non_numeric_suffix():
    assert canonical_event_id("EVT-SUMMER") == "evt-summer"


def test_event_ids_equivalent():
    assert event_ids_equivalent("EVT-9", "9") is True
    assert event_ids_equivalent("evt_9", "EVT9") is True
    assert event_ids_equivalent("DELIVERY-1", "delivery-1") is True
    assert event_ids_equivalent("DELIVERY-1", "DELIVERY-2") is False


def test_sql_in_values_digit_canon():
    v = event_id_lowercase_sql_in_values("42")
    assert "42" in v
    assert "evt-42" in v
    assert "evt_42" in v
    assert "evt42" in v


def test_merge_event_id_for_arrival_accepts_canonical_equivalent():
    merged, err = merge_event_id_for_arrival(token_event_id="EVT-100", payload_event_id="100")
    assert err is None
    assert merged == "EVT-100"

    merged2, err2 = merge_event_id_for_arrival(token_event_id=None, payload_event_id="evt_7")
    assert err2 is None
    assert merged2 == "evt_7"

    _, err3 = merge_event_id_for_arrival(token_event_id="EVT-1", payload_event_id="2")
    assert err3 is not None
    assert err3["error"] == "EVENT_MISMATCH"
