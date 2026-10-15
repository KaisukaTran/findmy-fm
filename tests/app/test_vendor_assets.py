"""Vendored third-party assets stay pinned, licensed, and compatible with the strict CSP."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

_VENDOR = Path(__file__).resolve().parents[2] / "app" / "static" / "vendor"
_LWC = _VENDOR / "lightweight-charts-5.2.1.standalone.production.js"
_LWC_SHA256 = "e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf"
_HTMX = _VENDOR / "htmx-2.0.3.min.js"
_HTMX_SHA256 = "491955cd1810747d7d7b9ccb936400afb760e06d25d53e4572b64b6563b2784e"
_ALPINE = _VENDOR / "alpine-csp-3.14.1.min.js"
_ALPINE_SHA256 = "40f00d7aaa543eabd17c3a0f79ce2ac62e6be17caa9564527c20ec39152bb157"


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


def test_htmx_and_alpine_hashes_pinned():
    """Byte-identical to npm htmx.org@2.0.3 dist/htmx.min.js and @alpinejs/csp@3.14.1
    dist/cdn.min.js. .gitattributes (vendor/** -text) keeps autocrlf from rewriting them."""
    assert hashlib.sha256(_HTMX.read_bytes()).hexdigest() == _HTMX_SHA256
    assert hashlib.sha256(_ALPINE.read_bytes()).hexdigest() == _ALPINE_SHA256


def test_htmx_and_alpine_licenses_and_docs_present():
    assert "Zero-Clause BSD" in (_VENDOR / "htmx.LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in (_VENDOR / "alpine.LICENSE").read_text(encoding="utf-8")
    doc = (_VENDOR / "VENDOR.md").read_text(encoding="utf-8")
    for name, digest in ((_HTMX.name, _HTMX_SHA256), (_ALPINE.name, _ALPINE_SHA256)):
        assert name in doc and digest in doc


def test_old_unversioned_copies_are_gone():
    static = _VENDOR.parent
    assert not (static / "htmx.min.js").exists()
    assert not (static / "alpine.min.js").exists()


def test_dashboard_loads_scripts_in_the_deferred_order():
    """Deferred scripts run in document order: charts lib, app.js (registers the Alpine
    components and reads no htmx config), htmx, then Alpine last so it starts on a DOM
    where every x-data component is already registered."""
    with TestClient(app) as c:
        html = c.get("/").text
        order = [
            "/static/vendor/lightweight-charts-5.2.1.standalone.production.js",
            "/static/app.js",
            "/static/vendor/htmx-2.0.3.min.js",
            "/static/vendor/alpine-csp-3.14.1.min.js",
        ]
        idx = [html.index(src) for src in order]
        assert idx == sorted(idx)
        for src in order[2:]:
            assert f'<script defer src="{src}"></script>' in html
            assert c.get(src).status_code == 200
        assert "/static/htmx.min.js" not in html and "/static/alpine.min.js" not in html
