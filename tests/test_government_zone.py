"""Government local area code resolution."""
from unittest.mock import patch

from app.services.government_zone_service import (
    is_valid_local_code_format,
    normalize_local_area_code,
    resolve_government_address,
    resolve_government_local_code,
)


def test_normalize_postal_code():
    assert normalize_local_area_code("  10110  ") == "10110"


def test_postal_code_format():
    assert is_valid_local_code_format("10110")
    assert is_valid_local_code_format("ID-JK-3171")
    assert not is_valid_local_code_format("ab")


@patch("app.services.government_zone_service.lookup_area_boundary", return_value=None)
def test_resolve_catalog_postal(_mock_osm):
    resolution = resolve_government_local_code(None, [], "10110")
    assert resolution is not None
    assert resolution.reference_id == "10110"
    assert resolution.source == "catalog"
    assert resolution.config.get("code_type") == "postal"
    assert resolution.geometry.get("geo_fence_polygon") is not None


@patch("app.services.government_zone_service.lookup_area_boundary", return_value=None)
def test_resolve_district_code(_mock_osm):
    resolution = resolve_government_local_code(None, [], "ID-JK-3173")
    assert resolution is not None
    assert resolution.config.get("code_type") == "district"


@patch("app.services.government_zone_service.lookup_area_boundary")
def test_resolve_prefers_osm_polygon(mock_lookup):
    mock_lookup.return_value = (
        {
            "type": "Polygon",
            "coordinates": [
                [
                    [106.82, -6.17],
                    [106.84, -6.171],
                    [106.85, -6.18],
                    [106.83, -6.19],
                    [106.82, -6.17],
                ]
            ],
        },
        "Gambir (Postal 10110)",
    )
    resolution = resolve_government_local_code(None, [], "10110")
    assert resolution is not None
    assert resolution.source == "osm_boundary"
    ring = resolution.geometry["geo_fence_polygon"]["coordinates"][0]
    assert len(ring) == 5


@patch("app.services.government_zone_service.lookup_global_area_boundary")
def test_resolve_global_address(mock_lookup):
    mock_lookup.return_value = (
        {
            "type": "Polygon",
            "coordinates": [
                [
                    [24.94, 60.19],
                    [24.95, 60.19],
                    [24.95, 60.20],
                    [24.94, 60.20],
                    [24.94, 60.19],
                ]
            ],
        },
        "M5H 2N2, Toronto, Ontario, Canada",
        {
            "local_code": "ca|M5H 2N2|TORONTO",
            "postal_code": "M5H 2N2",
            "city": "Toronto",
            "country": "Canada",
            "country_code": "ca",
        },
    )
    resolution = resolve_government_address(
        None,
        [],
        {
            "address_mode": "postal",
            "postal_code": "M5H 2N2",
            "city": "Toronto",
            "country": "Canada",
        },
    )
    assert resolution is not None
    assert resolution.reference_id == "ca|M5H 2N2|TORONTO"
    assert resolution.source == "osm_boundary"
