"""Unit tests for guest-access QR URL helpers (no DB — integration checklist is in README)."""

from __future__ import annotations

from app.core.config import settings
from app.services import guest_access_qr


def test_guest_access_path_zid_only():
    assert guest_access_qr.guest_access_path_with_query("ZN-1XOJPP") == "/access?zid=ZN-1XOJPP"


def test_guest_access_path_with_eid():
    path = guest_access_qr.guest_access_path_with_query("ZN-1XOJPP", "EVT-01")
    assert path == "/access?zid=ZN-1XOJPP&eid=EVT-01"


def test_guest_access_path_percent_encodes_space():
    path = guest_access_qr.guest_access_path_with_query("ZN X")
    assert "zid=" in path
    assert "%20" in path


def test_absolute_url_uses_guest_env_first(monkeypatch):
    monkeypatch.setattr(settings, "GUEST_ACCESS_APP_BASE_URL", "https://app.example.com")
    monkeypatch.setattr(settings, "PUBLIC_WEB_APP_URL", "https://ignored.example.com")
    url = guest_access_qr.guest_access_absolute_url("Z1", None)
    assert url == "https://app.example.com/access?zid=Z1"


def test_absolute_url_falls_back_public_web_app_url(monkeypatch):
    monkeypatch.setattr(settings, "GUEST_ACCESS_APP_BASE_URL", "")
    monkeypatch.setattr(settings, "PUBLIC_WEB_APP_URL", "https://legacy.example.com/")
    url = guest_access_qr.guest_access_absolute_url("Z1", "E")
    assert url == "https://legacy.example.com/access?zid=Z1&eid=E"


def test_absolute_url_none_without_base(monkeypatch):
    monkeypatch.setattr(settings, "GUEST_ACCESS_APP_BASE_URL", "")
    monkeypatch.setattr(settings, "PUBLIC_WEB_APP_URL", "")
    assert guest_access_qr.guest_access_absolute_url("Z1") is None


def test_guest_access_path_with_gt():
    p = guest_access_qr.guest_access_path_with_guest_token("abc_xyz")
    assert p.startswith("/access?")
    assert "gt=" in p


def test_guest_access_absolute_url_with_gt(monkeypatch):
    monkeypatch.setattr(settings, "GUEST_ACCESS_APP_BASE_URL", "https://app.example.com")
    assert guest_access_qr.guest_access_absolute_url_with_guest_token("tok") == "https://app.example.com/access?gt=tok"


def test_guest_access_path_gt_includes_zone_and_event():
    p = guest_access_qr.guest_access_path_with_guest_token("secret", zone_id="ZN-1", event_id="EVT99")
    assert p == "/access?gt=secret&zid=ZN-1&eid=EVT99"


def test_guest_access_absolute_url_gt_with_zone(monkeypatch):
    monkeypatch.setattr(settings, "GUEST_ACCESS_APP_BASE_URL", "https://app.example.com")
    u = guest_access_qr.guest_access_absolute_url_with_guest_token(
        "tok",
        zone_id="Z-zone",
        event_id=None,
    )
    assert u == "https://app.example.com/access?gt=tok&zid=Z-zone"


def test_guest_access_path_with_network_id():
    assert guest_access_qr.guest_access_path_with_network_id("NET-ABC") == "/access?nid=NET-ABC"


def test_guest_access_path_with_network_token():
    p = guest_access_qr.guest_access_path_with_network_token("secret", network_id="NET-1")
    assert p == "/access?gt=secret&zid=NET-1&nid=NET-1"


def test_guest_access_absolute_url_with_network_id(monkeypatch):
    monkeypatch.setattr(settings, "GUEST_ACCESS_APP_BASE_URL", "https://app.example.com")
    assert (
        guest_access_qr.guest_access_absolute_url_with_network_id("NET-1")
        == "https://app.example.com/access?nid=NET-1"
    )


def test_qr_png_non_empty():
    png = guest_access_qr.qr_png_bytes_for_url("https://example.com/access?zid=Z")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
