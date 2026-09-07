"""Communal reference existence checks and ID generation."""
from app.services.communal_zone_service import (
    generate_communal_reference,
    generate_unique_communal_id,
    is_valid_reference_format,
    normalize_reference_id,
    resolve_communal_reference,
)


def test_normalize_reference_id():
    assert normalize_reference_id("  comm-77  ") == "COMM-77"


def test_reference_format_validation():
    assert is_valid_reference_format("COMM-77")
    assert not is_valid_reference_format("ab")
    assert not is_valid_reference_format("")


def test_resolve_missing_without_db():
    resolution = resolve_communal_reference(None, [], "COMM-77")
    assert resolution is not None
    assert resolution["reference_id"] == "COMM-77"
    assert resolution["exists"] is False
    assert resolution["valid"] is False
    assert resolution["zones"] == []


def test_generate_unique_reference():
    resolution = generate_communal_reference(None, [])
    assert resolution["reference_id"].startswith("COMM-")
    assert is_valid_reference_format(resolution["reference_id"])
    assert resolution["exists"] is False
    assert resolution["geometry"] == {}


def test_generate_unique_communal_id_format():
    value = generate_unique_communal_id(None)
    assert value.startswith("COMM-")
    assert is_valid_reference_format(value)
