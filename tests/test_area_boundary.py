"""Area boundary lookup (mocked Nominatim)."""
from unittest.mock import patch

from app.services.area_boundary_service import (
    AreaLocationInput,
    _normalize_geojson_boundary,
    lookup_area_boundary,
    lookup_global_area_boundary,
    parse_area_location_from_dict,
)


def test_resolve_country_via_nominatim():
    from app.services.area_boundary_service import resolve_country_iso2

    assert resolve_country_iso2("Poland") == "pl"
    assert resolve_country_iso2("Suomi") == "fi"


def test_parse_toronto_postal():
    location = parse_area_location_from_dict(
        {
            "address_mode": "postal",
            "postal_code": "M5H 2N2",
            "city": "Toronto",
            "country": "Canada",
        }
    )
    assert location is not None
    assert location.reference_id() == "ca|M5H 2N2|TORONTO"


def test_build_reference_id_street():
    location = AreaLocationInput(
        country="Canada",
        city="Toronto",
        postal_code="M5H 2N2",
        street="Queen Street West",
        street_number="100",
        address_mode="street",
    )
    assert location.reference_id() == "ca|QUEEN STREET WEST|100|M5H 2N2|TORONTO"
    assert "M5H 2N2" in location.display_label()


def test_normalize_simplifies_large_polygon():
    ring = [[i, i] for i in range(800)] + [[0, 0]]
    geojson = {"type": "Polygon", "coordinates": [ring]}
    normalized = _normalize_geojson_boundary(geojson)
    assert normalized is not None
    assert normalized["type"] == "Polygon"
    assert len(normalized["coordinates"][0]) <= 513


@patch("app.services.area_boundary_service._nominatim_get")
def test_lookup_postal_returns_polygon(mock_get):
    mock_get.return_value = [
        {
            "display_name": "Kelurahan Gambir, Jakarta",
            "geojson": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [106.82, -6.17],
                        [106.83, -6.17],
                        [106.83, -6.18],
                        [106.82, -6.18],
                        [106.82, -6.17],
                    ]
                ],
            },
        }
    ]
    result = lookup_area_boundary("10110", code_type="postal")
    assert result is not None
    polygon, name = result
    assert polygon["type"] == "Polygon"
    assert len(polygon["coordinates"][0]) >= 4
    assert "Gambir" in name or "10110" in name


@patch("app.services.area_boundary_service._search_location_boundary")
@patch("app.services.area_boundary_service.resolve_country_iso2", return_value="fi")
def test_lookup_global_helsinki_postal(mock_resolve, mock_search):
    mock_search.return_value = {
        "display_name": "00510, Vallila, Helsinki, Finland",
        "geojson": {
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
    }
    location = AreaLocationInput(
        country="Finland",
        city="Helsinki",
        postal_code="00510",
        address_mode="postal",
    )
    result = lookup_global_area_boundary(location)
    assert result is not None
    polygon, name, config = result
    assert polygon["type"] == "Polygon"
    assert config["country_code"] == "fi"
    assert config["postal_code"] == "00510"
    mock_search.assert_called_once()


@patch("app.services.area_boundary_service._nominatim_get")
def test_lookup_global_toronto_postal(mock_get):
    mock_get.return_value = [
        {
            "display_name": "M5H 2N2, Toronto, Ontario, Canada",
            "geojson": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-79.39, 43.65],
                        [-79.38, 43.65],
                        [-79.38, 43.66],
                        [-79.39, 43.66],
                        [-79.39, 43.65],
                    ]
                ],
            },
        }
    ]
    location = AreaLocationInput(
        country="Canada",
        city="Toronto",
        postal_code="M5H 2N2",
        address_mode="postal",
    )
    result = lookup_global_area_boundary(location)
    assert result is not None
    polygon, name, config = result
    assert polygon["type"] == "Polygon"
    assert config["country_code"] == "ca"
    assert config["postal_code"] == "M5H 2N2"
    assert "Toronto" in name or "M5H" in name
