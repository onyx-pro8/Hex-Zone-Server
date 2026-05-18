"""Fetch administrative area boundaries from OpenStreetMap (Nominatim), globally."""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
PHOTON_SEARCH_URL = "https://photon.komoot.io/api/"

_NOMINATIM_MIN_INTERVAL_SEC = 1.05
_last_nominatim_request_at = 0.0
_last_lookup_failure = ""

# Legacy catalog / district shortcuts (optional fallback).
GOVERNMENT_CODE_QUERIES: dict[str, str] = {
    "ID-JK-3171": "Jakarta Selatan, Daerah Khusus Ibukota Jakarta, Indonesia",
    "ID-JK-3173": "Jakarta Timur, Daerah Khusus Ibukota Jakarta, Indonesia",
    "DIST-JKT-CENTRAL": "Jakarta Pusat, Daerah Khusus Ibukota Jakarta, Indonesia",
}

# Common aliases; any other name is resolved via Nominatim (see resolve_country_iso2).
_COUNTRY_NAME_TO_ISO2: dict[str, str] = {
    "INDONESIA": "id",
    "FINLAND": "fi",
    "SUOMI": "fi",
    "UNITED STATES": "us",
    "UNITED STATES OF AMERICA": "us",
    "USA": "us",
    "UNITED KINGDOM": "gb",
    "UK": "gb",
    "GERMANY": "de",
    "FRANCE": "fr",
    "AUSTRALIA": "au",
    "JAPAN": "jp",
    "SINGAPORE": "sg",
    "MALAYSIA": "my",
    "NETHERLANDS": "nl",
    "SWEDEN": "se",
    "NORWAY": "no",
    "DENMARK": "dk",
    "CANADA": "ca",
    "UKRAINE": "ua",
    "INDIA": "in",
    "CHINA": "cn",
    "SOUTH KOREA": "kr",
    "KOREA": "kr",
    "THAILAND": "th",
    "PHILIPPINES": "ph",
    "VIETNAM": "vn",
    "SPAIN": "es",
    "ITALY": "it",
    "BRAZIL": "br",
    "MEXICO": "mx",
    "POLAND": "pl",
    "PORTUGAL": "pt",
    "BELGIUM": "be",
    "AUSTRIA": "at",
    "SWITZERLAND": "ch",
    "IRELAND": "ie",
    "NEW ZEALAND": "nz",
    "SOUTH AFRICA": "za",
    "ARGENTINA": "ar",
    "CHILE": "cl",
    "COLOMBIA": "co",
    "TURKEY": "tr",
    "TÜRKIYE": "tr",
    "TURKIYE": "tr",
    "ISRAEL": "il",
    "EGYPT": "eg",
    "SAUDI ARABIA": "sa",
    "UNITED ARAB EMIRATES": "ae",
    "UAE": "ae",
}

_MAX_RING_VERTICES = 512
_COUNTRY_ISO_CACHE: dict[str, str] = {}
_LEGACY_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{2,63}$")


@dataclass(frozen=True)
class AreaLocationInput:
    """Global address for postal-area or street-level boundary lookup."""

    country: str
    city: str = ""
    postal_code: str = ""
    street: str = ""
    street_number: str = ""
    address_mode: str = "postal"  # postal | street | legacy

    def reference_id(self) -> str:
        country = resolve_country_iso2(self.country) or self.country.strip().upper()
        city = _clean_part(self.city).upper()
        postal = _clean_part(self.postal_code).upper()
        if self.address_mode == "street":
            street = _clean_part(self.street).upper()
            number = _clean_part(self.street_number).upper()
            return "|".join([country, street, number, postal, city])
        if postal and city:
            return "|".join([country, postal, city])
        if postal:
            return f"{country}|{postal}"
        return country

    def display_label(self) -> str:
        parts: list[str] = []
        if self.street.strip():
            line = self.street.strip()
            if self.street_number.strip():
                line = f"{line} {self.street_number.strip()}"
            parts.append(line)
        if self.postal_code.strip():
            parts.append(self.postal_code.strip())
        if self.city.strip():
            parts.append(self.city.strip())
        if self.country.strip():
            parts.append(self.country.strip())
        return ", ".join(parts) if parts else self.reference_id()

    def to_config(self) -> dict[str, Any]:
        iso2 = resolve_country_iso2(self.country)
        return {
            "local_code": self.reference_id(),
            "area_code": self.reference_id(),
            "address_mode": self.address_mode,
            "postal_code": self.postal_code.strip(),
            "city": self.city.strip(),
            "country": self.country.strip(),
            "country_code": iso2 or "",
            "street": self.street.strip(),
            "street_number": self.street_number.strip(),
            "code_type": "street" if self.address_mode == "street" else "postal",
        }


def _clean_part(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _user_agent() -> str:
    return getattr(settings, "NOMINATIM_USER_AGENT", None) or "HexZone/1.0 (area-boundary-lookup)"


def _boundary_lookup_enabled() -> bool:
    return bool(settings.BOUNDARY_LOOKUP_ENABLED)


def get_last_boundary_lookup_failure() -> str:
    return _last_lookup_failure


def _set_lookup_failure(reason: str) -> None:
    global _last_lookup_failure
    _last_lookup_failure = reason


def _wait_nominatim_rate_limit() -> None:
    global _last_nominatim_request_at
    elapsed = time.monotonic() - _last_nominatim_request_at
    if elapsed < _NOMINATIM_MIN_INTERVAL_SEC:
        time.sleep(_NOMINATIM_MIN_INTERVAL_SEC - elapsed)
    _last_nominatim_request_at = time.monotonic()


def normalize_country_iso2(country: str) -> Optional[str]:
    """Map a country label to ISO 3166-1 alpha-2 when possible (static list only)."""
    raw = _clean_part(country)
    if not raw:
        return None
    upper = raw.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper.lower()
    return _COUNTRY_NAME_TO_ISO2.get(upper)


def resolve_country_iso2(country: str) -> Optional[str]:
    """Resolve country to ISO2 via static map, cache, or Nominatim country search."""
    iso2 = normalize_country_iso2(country)
    if iso2:
        return iso2
    raw = _clean_part(country)
    if not raw:
        return None
    cache_key = raw.upper()
    if cache_key in _COUNTRY_ISO_CACHE:
        return _COUNTRY_ISO_CACHE[cache_key]
    try:
        results = _nominatim_get(
            NOMINATIM_SEARCH_URL,
            {
                "format": "json",
                "limit": 3,
                "q": raw,
                "featureType": "country",
            },
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Nominatim country lookup failed for %s: %s", raw, exc)
        return None
    for row in results:
        address = row.get("address")
        if not isinstance(address, dict):
            continue
        code = address.get("country_code")
        if isinstance(code, str) and len(code) == 2:
            resolved = code.lower()
            _COUNTRY_ISO_CACHE[cache_key] = resolved
            return resolved
    return None


def boundary_lookup_status() -> tuple[bool, str]:
    """Whether OSM boundary lookup is enabled; second value is a short reason if disabled."""
    if not _boundary_lookup_enabled():
        return False, "boundary_lookup_disabled"
    return True, ""


def is_valid_legacy_area_code(code: str) -> bool:
    normalized = re.sub(r"\s+", "", str(code or "").strip()).upper()
    return bool(normalized) and bool(_LEGACY_CODE_PATTERN.match(normalized))


def _simplify_ring(ring: list[list[float]], max_vertices: int = _MAX_RING_VERTICES) -> list[list[float]]:
    if len(ring) <= max_vertices:
        return ring
    step = max(1, math.ceil((len(ring) - 1) / (max_vertices - 1)))
    sampled = ring[::step]
    if sampled[-1] != ring[-1]:
        sampled.append(ring[-1])
    if sampled[0] != sampled[-1]:
        sampled.append(sampled[0])
    return sampled


def _normalize_geojson_boundary(geojson: dict[str, Any]) -> Optional[dict[str, Any]]:
    geometry_type = geojson.get("type")
    if geometry_type == "Polygon":
        coords = geojson.get("coordinates")
        if not isinstance(coords, list) or not coords:
            return None
        rings = []
        for ring in coords:
            if not isinstance(ring, list) or len(ring) < 4:
                continue
            rings.append(_simplify_ring(ring))
        if not rings:
            return None
        return {"type": "Polygon", "coordinates": rings}
    if geometry_type == "MultiPolygon":
        coords = geojson.get("coordinates")
        if not isinstance(coords, list):
            return None
        polys = []
        for poly in coords:
            if not isinstance(poly, list) or not poly:
                continue
            rings = []
            for ring in poly:
                if isinstance(ring, list) and len(ring) >= 4:
                    rings.append(_simplify_ring(ring))
            if rings:
                polys.append(rings)
        if not polys:
            return None
        if len(polys) == 1:
            return {"type": "Polygon", "coordinates": polys[0]}
        return {"type": "MultiPolygon", "coordinates": polys}
    return None


def _polygon_vertex_count(geojson: dict[str, Any]) -> int:
    geometry_type = geojson.get("type")
    if geometry_type == "Polygon":
        coords = geojson.get("coordinates")
        if isinstance(coords, list) and coords and isinstance(coords[0], list):
            return len(coords[0])
    if geometry_type == "MultiPolygon":
        coords = geojson.get("coordinates")
        if isinstance(coords, list) and coords and isinstance(coords[0], list):
            return max((len(ring) for ring in coords[0] if isinstance(ring, list)), default=0)
    return 0


def _ring_bbox_area(ring: list[list[float]]) -> float:
    if len(ring) < 4:
        return 0.0
    lngs = [p[0] for p in ring if isinstance(p, list) and len(p) >= 2]
    lats = [p[1] for p in ring if isinstance(p, list) and len(p) >= 2]
    if not lngs or not lats:
        return 0.0
    return abs(max(lngs) - min(lngs)) * abs(max(lats) - min(lats))


def _polygon_area_estimate(geojson: dict[str, Any]) -> float:
    geometry_type = geojson.get("type")
    if geometry_type == "Polygon":
        coords = geojson.get("coordinates")
        if isinstance(coords, list) and coords and isinstance(coords[0], list):
            return _ring_bbox_area(coords[0])
    if geometry_type == "MultiPolygon":
        coords = geojson.get("coordinates")
        if isinstance(coords, list) and coords and isinstance(coords[0], list):
            rings = coords[0]
            if rings and isinstance(rings[0], list):
                return _ring_bbox_area(rings[0])
    return 0.0


def _pick_polygon_result(
    results: list[dict[str, Any]],
    *,
    prefer_smallest: bool = False,
) -> Optional[dict[str, Any]]:
    candidates: list[tuple[float, int, dict[str, Any]]] = []
    for row in results:
        geojson = row.get("geojson")
        if not isinstance(geojson, dict) or geojson.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        count = _polygon_vertex_count(geojson)
        if count < 4:
            continue
        area = _polygon_area_estimate(geojson)
        candidates.append((area, count, row))
    if not candidates:
        return None
    if prefer_smallest:
        candidates.sort(key=lambda item: (item[0], item[1]))
    else:
        candidates.sort(key=lambda item: (-item[1], -item[0]))
    return candidates[0][2]


def _pick_postcode_or_place_row(results: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for row in results:
        if row.get("type") in {"postcode", "postal_code"}:
            return row
        if row.get("class") == "place" and row.get("type") in {"postcode", "suburb", "neighbourhood", "quarter"}:
            return row
    return results[0] if results else None


def _nominatim_get(url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    _wait_nominatim_rate_limit()
    headers = {"User-Agent": _user_agent(), "Accept": "application/json"}
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        _set_lookup_failure(f"nominatim_http_{exc.response.status_code}")
        raise
    except httpx.HTTPError as exc:
        _set_lookup_failure(f"nominatim_unreachable:{type(exc).__name__}")
        raise
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _photon_geocode(query: str) -> Optional[tuple[float, float, str]]:
    """Forward geocode via Photon (Komoot). Returns (lat, lon, label)."""
    q = _clean_part(query)
    if not q:
        return None
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.get(
                PHOTON_SEARCH_URL,
                params={"q": q, "limit": 1, "lang": "en"},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        logger.warning("Photon geocode failed for %s: %s", q, exc)
        _set_lookup_failure(f"photon_unreachable:{type(exc).__name__}")
        return None
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list) or not features:
        _set_lookup_failure("photon_no_results")
        return None
    feature = features[0]
    if not isinstance(feature, dict):
        return None
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    lon = float(coordinates[0])
    lat = float(coordinates[1])
    props = feature.get("properties")
    if not isinstance(props, dict):
        return lat, lon, q
    city = props.get("city") or props.get("town") or props.get("village") or ""
    parts = [
        str(props.get("name") or "").strip(),
        str(props.get("street") or "").strip(),
        str(props.get("postcode") or "").strip(),
        str(city).strip(),
        str(props.get("country") or "").strip(),
    ]
    label = ", ".join(p for p in parts if p) or q
    return lat, lon, label


def _square_buffer_polygon(
    lon: float,
    lat: float,
    *,
    half_km: float = 1.2,
) -> dict[str, Any]:
    """Approximate postcode/neighbourhood box when OSM has no polygon."""
    dlat = half_km / 111.0
    cos_lat = max(0.2, abs(math.cos(math.radians(lat))))
    dlon = half_km / (111.0 * cos_lat)
    ring = [
        [lon - dlon, lat - dlat],
        [lon + dlon, lat - dlat],
        [lon + dlon, lat + dlat],
        [lon - dlon, lat + dlat],
        [lon - dlon, lat - dlat],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def _reverse_admin_polygon(
    lat: str,
    lon: str,
    *,
    zoom: int = 12,
) -> Optional[dict[str, Any]]:
    params = {
        "lat": lat,
        "lon": lon,
        "format": "json",
        "polygon_geojson": 1,
        "zoom": zoom,
        "addressdetails": 1,
    }
    try:
        results = _nominatim_get(NOMINATIM_REVERSE_URL, params)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Nominatim reverse failed at %s,%s: %s", lat, lon, exc)
        return None
    if not results:
        return None
    row = results[0]
    geojson = row.get("geojson")
    if isinstance(geojson, dict) and geojson.get("type") in {"Polygon", "MultiPolygon"}:
        return row
    return None


def _reverse_polygon_at_point(
    lat: str,
    lon: str,
    *,
    prefer_postal: bool = False,
) -> Optional[dict[str, Any]]:
    zooms = [18, 16, 14, 12, 10] if prefer_postal else [14, 12, 10]
    for zoom in zooms:
        row = _reverse_admin_polygon(lat, lon, zoom=zoom)
        if row is not None:
            return row
    return None


def _search_nominatim(
    *,
    country_iso2: str | None = None,
    postal_code: str | None = None,
    city: str | None = None,
    country_name: str | None = None,
    street: str | None = None,
    free_query: str | None = None,
    prefer_smallest_polygon: bool = False,
    prefer_postal_point: bool = False,
) -> Optional[dict[str, Any]]:
    params: dict[str, Any] = {
        "format": "json",
        "polygon_geojson": 1,
        "limit": 10,
        "addressdetails": 1,
    }
    if country_iso2:
        params["countrycodes"] = country_iso2
    if free_query:
        params["q"] = free_query
    else:
        if street:
            params["street"] = street
        if postal_code:
            params["postalcode"] = postal_code
        if city:
            params["city"] = city
        if country_name:
            params["country"] = country_name

    try:
        results = _nominatim_get(NOMINATIM_SEARCH_URL, params)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Nominatim search failed: %s", exc)
        return None

    picked = _pick_polygon_result(results, prefer_smallest=prefer_smallest_polygon)
    if picked:
        return picked

    point_row = _pick_postcode_or_place_row(results) if results else None
    if point_row:
        lat = point_row.get("lat")
        lon = point_row.get("lon")
        if lat is not None and lon is not None:
            reversed_row = _reverse_polygon_at_point(
                str(lat),
                str(lon),
                prefer_postal=prefer_postal_point or bool(postal_code),
            )
            if reversed_row:
                return reversed_row

    return None


def _search_location_boundary(
    location: AreaLocationInput,
    *,
    country_iso2: str | None,
) -> Optional[dict[str, Any]]:
    """Try several Nominatim strategies until a polygon is found."""
    country_name = location.country.strip()
    city = location.city.strip()
    postal = location.postal_code.strip()
    label = location.display_label()
    prefer_postal = location.address_mode != "street" and bool(postal)

    street_line: str | None = None
    if location.address_mode == "street":
        street_parts = [location.street.strip()]
        if location.street_number.strip():
            street_parts.append(location.street_number.strip())
        street_line = " ".join(p for p in street_parts if p)

    strategies: list[dict[str, Any]] = []

    if label:
        strategies.append(
            {
                "country_iso2": country_iso2,
                "free_query": label,
                "prefer_smallest_polygon": prefer_postal,
                "prefer_postal_point": prefer_postal,
            }
        )
        if country_iso2:
            strategies.append(
                {
                    "country_iso2": None,
                    "free_query": label,
                    "prefer_smallest_polygon": prefer_postal,
                    "prefer_postal_point": prefer_postal,
                }
            )

    if location.address_mode == "street" and street_line:
        strategies.append(
            {
                "country_iso2": country_iso2,
                "street": street_line,
                "postal_code": postal or None,
                "city": city or None,
                "country_name": country_name,
                "prefer_postal_point": True,
            }
        )
    elif postal or city:
        strategies.append(
            {
                "country_iso2": country_iso2,
                "postal_code": postal or None,
                "city": city or None,
                "country_name": country_name,
                "prefer_smallest_polygon": prefer_postal,
                "prefer_postal_point": prefer_postal,
            }
        )

    nominatim_failed = False
    for kwargs in strategies:
        try:
            row = _search_nominatim(**kwargs)
        except httpx.HTTPError:
            nominatim_failed = True
            continue
        if row:
            return row

    if label:
        geocoded = _photon_geocode(label)
        if geocoded:
            lat, lon, photon_name = geocoded
            try:
                reversed_row = _reverse_polygon_at_point(
                    str(lat),
                    str(lon),
                    prefer_postal=prefer_postal,
                )
            except httpx.HTTPError:
                reversed_row = None
                nominatim_failed = True
            if reversed_row:
                if isinstance(reversed_row.get("display_name"), str):
                    return reversed_row
                reversed_row = dict(reversed_row)
                reversed_row["display_name"] = photon_name
                return reversed_row
            half_km = 1.2 if prefer_postal else 2.0
            return {
                "display_name": photon_name,
                "geojson": _square_buffer_polygon(lon, lat, half_km=half_km),
                "_approximate": True,
            }

    if nominatim_failed:
        _set_lookup_failure(_last_lookup_failure or "nominatim_unreachable")
    else:
        _set_lookup_failure("no_boundary_polygon_found")
    return None


def lookup_global_area_boundary(
    location: AreaLocationInput,
) -> Optional[tuple[dict[str, Any], str, dict[str, Any]]]:
    """
    Resolve a global postal or street address to polygon + display name + config.

    Returns (geo_fence_polygon, display_name, config) or None.
    """
    _set_lookup_failure("")
    if not _boundary_lookup_enabled():
        _set_lookup_failure("boundary_lookup_disabled")
        return None

    if location.address_mode == "street":
        if not location.street.strip() and not location.postal_code.strip():
            _set_lookup_failure("missing_street_or_postal")
            return None
    elif not location.postal_code.strip() and not location.city.strip():
        _set_lookup_failure("missing_postal_or_city")
        return None

    iso2 = resolve_country_iso2(location.country)
    row = _search_location_boundary(location, country_iso2=iso2)
    if not row:
        return None

    geojson = row.get("geojson")
    if not isinstance(geojson, dict):
        _set_lookup_failure("invalid_geojson")
        return None
    polygon = _normalize_geojson_boundary(geojson)
    if not polygon:
        _set_lookup_failure("polygon_normalization_failed")
        return None

    display_name = row.get("display_name")
    name = (
        display_name.strip()
        if isinstance(display_name, str) and display_name.strip()
        else location.display_label()
    )
    config = location.to_config()
    if row.get("_approximate"):
        config["boundary_precision"] = "approximate"
    return polygon, name, config


def _legacy_catalog_query(code: str) -> Optional[str]:
    if code in GOVERNMENT_CODE_QUERIES:
        return GOVERNMENT_CODE_QUERIES[code]
    if re.fullmatch(r"\d{5}", code):
        return None
    if "-" in code:
        return f"{code.replace('-', ' ')}"
    return code


def lookup_area_boundary(
    code: str,
    *,
    code_type: str | None = None,
    country_iso2: str | None = None,
) -> Optional[tuple[dict[str, Any], str]]:
    """Legacy single-code lookup (catalog shortcuts + optional country hint)."""
    if not _boundary_lookup_enabled():
        return None

    normalized = re.sub(r"\s+", "", str(code or "").strip()).upper()
    if not normalized:
        return None

    iso2 = country_iso2 or "id"
    legacy_query = _legacy_catalog_query(normalized)
    if legacy_query and not country_iso2:
        row = _search_nominatim(country_iso2=iso2, free_query=f"{legacy_query}, Indonesia")
    elif legacy_query:
        row = _search_nominatim(country_iso2=iso2, free_query=legacy_query)
    else:
        row = _search_nominatim(
            country_iso2=iso2,
            postal_code=normalized if re.fullmatch(r"[\w\d-]{3,12}", normalized) else None,
            free_query=normalized,
        )

    if not row:
        return None

    geojson = row.get("geojson")
    if not isinstance(geojson, dict):
        return None
    polygon = _normalize_geojson_boundary(geojson)
    if not polygon:
        return None

    display_name = row.get("display_name")
    name = display_name.strip() if isinstance(display_name, str) and display_name.strip() else normalized
    return polygon, name


def parse_area_location_from_dict(data: dict[str, Any]) -> Optional[AreaLocationInput]:
    country = str(data.get("country") or data.get("country_name") or "").strip()
    if not country:
        return None
    mode = str(data.get("address_mode") or "postal").strip().lower()
    if mode not in {"postal", "street"}:
        mode = "street" if data.get("street") else "postal"
    return AreaLocationInput(
        country=country,
        city=str(data.get("city") or "").strip(),
        postal_code=str(data.get("postal_code") or data.get("postalcode") or "").strip(),
        street=str(data.get("street") or "").strip(),
        street_number=str(
            data.get("street_number") or data.get("streetNumber") or data.get("house_number") or ""
        ).strip(),
        address_mode=mode,
    )
