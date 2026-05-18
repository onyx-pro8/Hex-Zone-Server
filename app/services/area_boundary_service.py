"""Fetch administrative area boundaries from OpenStreetMap (Nominatim), globally."""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

# Legacy catalog / district shortcuts (optional fallback).
GOVERNMENT_CODE_QUERIES: dict[str, str] = {
    "ID-JK-3171": "Jakarta Selatan, Daerah Khusus Ibukota Jakarta, Indonesia",
    "ID-JK-3173": "Jakarta Timur, Daerah Khusus Ibukota Jakarta, Indonesia",
    "DIST-JKT-CENTRAL": "Jakarta Pusat, Daerah Khusus Ibukota Jakarta, Indonesia",
}

_COUNTRY_NAME_TO_ISO2: dict[str, str] = {
    "INDONESIA": "id",
    "FINLAND": "fi",
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
}

_MAX_RING_VERTICES = 512
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
    return bool(getattr(settings, "BOUNDARY_LOOKUP_ENABLED", True))


def normalize_country_iso2(country: str) -> Optional[str]:
    """Resolve ISO 3166-1 alpha-2 from a 2-letter code or known English country name."""
    raw = _clean_part(country)
    if not raw:
        return None
    upper = raw.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper.lower()
    return _COUNTRY_NAME_TO_ISO2.get(upper)


def resolve_country_iso2(country: str) -> Optional[str]:
    """
    Resolve ISO2 for any country: known names, 2-letter codes, then Nominatim country search.
    """
    iso2 = normalize_country_iso2(country)
    if iso2:
        return iso2
    raw = _clean_part(country)
    if not raw:
        return None
    try:
        results = _nominatim_get(
            NOMINATIM_SEARCH_URL,
            {
                "q": raw,
                "format": "json",
                "limit": 1,
                "addressdetails": 1,
                "featuretype": "country",
            },
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Nominatim country lookup failed for %r: %s", raw, exc)
        return None
    if not results:
        return None
    address = results[0].get("address")
    if isinstance(address, dict):
        code = address.get("country_code")
        if isinstance(code, str) and len(code) == 2:
            return code.lower()
    return None


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


def _result_postal_code(row: dict[str, Any]) -> str:
    address = row.get("address")
    if not isinstance(address, dict):
        return ""
    for key in ("postcode", "postal_code"):
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", "", value.strip()).upper()
    return ""


def _pick_polygon_result(
    results: list[dict[str, Any]],
    *,
    postal_code: str | None = None,
) -> Optional[dict[str, Any]]:
    """Prefer polygon results; for postcodes prefer matching or smaller areas."""
    normalized_postal = re.sub(r"\s+", "", str(postal_code or "").strip()).upper()
    polygon_rows: list[dict[str, Any]] = []
    for row in results:
        geojson = row.get("geojson")
        if not isinstance(geojson, dict) or geojson.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        if _polygon_vertex_count(geojson) >= 4:
            polygon_rows.append(row)

    if not polygon_rows:
        return None

    if normalized_postal:
        for row in polygon_rows:
            if _result_postal_code(row) == normalized_postal:
                return row

    # Prefer smaller boundaries (postcode/suburb) over city/region when multiple polygons exist.
    return min(polygon_rows, key=lambda row: _polygon_vertex_count(row.get("geojson", {})))


def _nominatim_get(url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    headers = {"User-Agent": _user_agent(), "Accept": "application/json"}
    with httpx.Client(timeout=25.0) as client:
        response = client.get(url, headers=headers, params=params)
        response.raise_for_status()
        payload = response.json()
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


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


def _search_nominatim(
    *,
    country_iso2: str | None = None,
    postal_code: str | None = None,
    city: str | None = None,
    country_name: str | None = None,
    street: str | None = None,
    free_query: str | None = None,
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

    picked = _pick_polygon_result(results, postal_code=postal_code)
    if picked:
        return picked

    if results:
        point_row = results[0]
        lat = point_row.get("lat")
        lon = point_row.get("lon")
        if lat is not None and lon is not None:
            zoom = 14 if street or postal_code else 12
            reversed_row = _reverse_admin_polygon(str(lat), str(lon), zoom=zoom)
            if reversed_row:
                return reversed_row

    return None


def _compose_free_query(
    *,
    postal_code: str = "",
    city: str = "",
    country: str = "",
    street: str = "",
) -> str:
    parts = [p for p in [street, postal_code, city, country] if p and str(p).strip()]
    return ", ".join(parts)


def _lookup_address_row(location: AreaLocationInput, *, iso2: str | None) -> Optional[dict[str, Any]]:
    """Try several Nominatim strategies until a boundary or reverse-geocode polygon is found."""
    country_name = location.country.strip()
    city = location.city.strip()
    postal = location.postal_code.strip()
    label = location.display_label()

    if location.address_mode == "street":
        street_parts = [location.street.strip()]
        if location.street_number.strip():
            street_parts.append(location.street_number.strip())
        street_line = " ".join(p for p in street_parts if p)
        if not street_line and not postal:
            return None
        attempts: list[dict[str, Any]] = []
        if iso2:
            attempts.append(
                {
                    "country_iso2": iso2,
                    "postal_code": postal or None,
                    "city": city or None,
                    "country_name": country_name,
                    "street": street_line or None,
                }
            )
        attempts.append({"free_query": label, "country_iso2": iso2})
        attempts.append({"free_query": label, "country_iso2": None})
    else:
        if not postal and not city:
            return None
        attempts = []
        if iso2:
            attempts.append(
                {
                    "country_iso2": iso2,
                    "postal_code": postal or None,
                    "city": city or None,
                    "country_name": country_name,
                }
            )
            if postal:
                attempts.append(
                    {
                        "country_iso2": iso2,
                        "postal_code": postal,
                        "country_name": country_name,
                    }
                )
        free_parts = _compose_free_query(postal_code=postal, city=city, country=country_name)
        if free_parts:
            attempts.append({"free_query": free_parts, "country_iso2": iso2})
            attempts.append({"free_query": free_parts, "country_iso2": None})
        if label and label != free_parts:
            attempts.append({"free_query": label, "country_iso2": None})

    seen: set[str] = set()
    for attempt in attempts:
        key = repr(sorted(attempt.items()))
        if key in seen:
            continue
        seen.add(key)
        row = _search_nominatim(**attempt)
        if row:
            return row
    return None


def lookup_global_area_boundary(
    location: AreaLocationInput,
) -> Optional[tuple[dict[str, Any], str, dict[str, Any]]]:
    """
    Resolve a global postal or street address to polygon + display name + config.

    Returns (geo_fence_polygon, display_name, config) or None.
    """
    if not _boundary_lookup_enabled():
        return None

    iso2 = resolve_country_iso2(location.country)
    row = _lookup_address_row(location, iso2=iso2)
    if not row:
        logger.info(
            "No boundary for %s (iso2=%s)",
            location.display_label(),
            iso2 or "unknown",
        )
        return None

    geojson = row.get("geojson")
    if not isinstance(geojson, dict):
        return None
    polygon = _normalize_geojson_boundary(geojson)
    if not polygon:
        return None

    display_name = row.get("display_name")
    name = (
        display_name.strip()
        if isinstance(display_name, str) and display_name.strip()
        else location.display_label()
    )
    return polygon, name, location.to_config()


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
