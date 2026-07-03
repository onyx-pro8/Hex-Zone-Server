"""Canonical guest-entry URLs for zone QR (deep link to SPA `/access`)."""

from __future__ import annotations

import io
from urllib.parse import quote, urlencode

from app.core.config import settings


def guest_access_web_base() -> str:
    """Public web app origin for `/access` (no trailing slash)."""
    raw = (settings.GUEST_ACCESS_APP_BASE_URL or settings.PUBLIC_WEB_APP_URL or "").strip()
    return raw.rstrip("/")


def build_guest_access_query_params(zone_id: str, event_id: str | None) -> dict[str, str]:
    zid = zone_id.strip()
    params: dict[str, str] = {"zid": zid}
    ev = (event_id or "").strip()
    if ev:
        params["eid"] = ev
    return params


def guest_access_path_with_query(zone_id: str, event_id: str | None = None) -> str:
    """Path and query only, e.g. `/access?zid=ZN-ABC`."""
    qs = urlencode(build_guest_access_query_params(zone_id, event_id), quote_via=quote)
    return f"/access?{qs}"


def build_guest_access_query_params_for_guest_token(
    secret_token: str,
    zone_id: str | None,
    event_id: str | None,
) -> dict[str, str]:
    """Query for issued QR tokens: **`gt`** plus **`zid`** (and **`eid`** when bound)."""
    tok = secret_token.strip()
    params: dict[str, str] = {"gt": tok}
    z = (zone_id or "").strip()
    if z:
        params["zid"] = z
    ev = (event_id or "").strip()
    if ev:
        params["eid"] = ev
    return params


def guest_access_path_with_guest_token(
    secret_token: str,
    *,
    zone_id: str | None = None,
    event_id: str | None = None,
) -> str:
    """Deep-link path: **`?gt=`**; when **zone_id** is set (server-mint URLs), **`zid`** (and optional **`eid`**) included."""
    qs = urlencode(
        build_guest_access_query_params_for_guest_token(secret_token, zone_id, event_id),
        quote_via=quote,
    )
    return f"/access?{qs}"


def guest_access_absolute_url_with_guest_token(
    secret_token: str,
    *,
    zone_id: str | None = None,
    event_id: str | None = None,
) -> str | None:
    base = guest_access_web_base()
    if not base:
        return None
    return f"{base}{guest_access_path_with_guest_token(secret_token, zone_id=zone_id, event_id=event_id)}"


def guest_access_absolute_url(zone_id: str, event_id: str | None = None) -> str | None:
    """Full HTTPS-ready URL if **GUEST_ACCESS_APP_BASE_URL** (or legacy **PUBLIC_WEB_APP_URL**) is set."""
    base = guest_access_web_base()
    if not base:
        return None
    return f"{base}{guest_access_path_with_query(zone_id, event_id)}"


def build_network_access_query_params(network_id: str) -> dict[str, str]:
    """Static network-id QR: **`/access?nid=`** (network id = ``owners.zone_id``)."""
    nid = network_id.strip()
    return {"nid": nid}


def guest_access_path_with_network_id(network_id: str) -> str:
    qs = urlencode(build_network_access_query_params(network_id), quote_via=quote)
    return f"/access?{qs}"


def guest_access_absolute_url_with_network_id(network_id: str) -> str | None:
    base = guest_access_web_base()
    if not base:
        return None
    return f"{base}{guest_access_path_with_network_id(network_id)}"


def build_network_access_query_params_for_token(
    secret_token: str,
    network_id: str,
) -> dict[str, str]:
    """Issued network-access token: **`gt`** + **`nid`**."""
    params = build_guest_access_query_params_for_guest_token(secret_token, network_id, None)
    params["nid"] = network_id.strip()
    return params


def guest_access_path_with_network_token(
    secret_token: str,
    *,
    network_id: str,
) -> str:
    qs = urlencode(
        build_network_access_query_params_for_token(secret_token, network_id),
        quote_via=quote,
    )
    return f"/access?{qs}"


def guest_access_absolute_url_with_network_token(
    secret_token: str,
    *,
    network_id: str,
) -> str | None:
    base = guest_access_web_base()
    if not base:
        return None
    return f"{base}{guest_access_path_with_network_token(secret_token, network_id=network_id)}"


def qr_png_bytes_for_url(url: str, *, box_size: int = 8, border: int = 2) -> bytes:
    """PNG bytes encoding **url** (install **qrcode** + **Pillow**)."""
    import qrcode

    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=border)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
