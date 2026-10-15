"""Vendored third-party assets stay pinned, licensed, and compatible with the strict CSP."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

_VENDOR = Path(__file__).resolve().parents[2] / "app" / "static" / "vendor"
_LWC = _VENDOR / "lightweight-charts-5.2.1.standalone.production.js"
_LWC_SHA256 = "e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf"


def test_lightweight_charts_hash_pinned():
    assert hashlib.sha256(_LWC.read_bytes()).hexdigest() == _LWC_SHA256


def test_vendor_docs_and_license_present():
    assert (_VENDOR / "VENDOR.md").is_file()
    lic = _VENDOR / "lightweight-charts.LICENSE"
    assert lic.is_file() and "Apache" in lic.read_text(encoding="utf-8", errors="replace")


def test_dashboard_loads_vendor_script_before_app_js():
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        html = r.text
        tag = "/static/vendor/lightweight-charts-5.2.1.standalone.production.js"
        assert tag in html
        assert html.index(tag) < html.index("/static/app.js")
        assert c.get(tag).status_code == 200


def test_csp_stays_strict():
    with TestClient(app) as c:
        csp = c.get("/").headers["content-security-policy"]
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "http:" not in csp and "https:" not in csp and "//" not in csp
