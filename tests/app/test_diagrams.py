"""The "Sơ đồ" tab: archify diagrams served into a sandboxed iframe.

Archify's viewer is one self-contained HTML file with inline <script>/<style>, which the app's
global CSP (script-src 'self', X-Frame-Options DENY) would block. The route relaxes the policy
for exactly these files and nothing else, and compensates with a CSP `sandbox` (opaque origin:
no cookies, no same-origin API calls) plus `connect-src 'none'`.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import diagrams, portfolio
from app.main import app as fastapi_app
from app.security import install_security


@pytest.fixture
def diagram_client():
    app = FastAPI()
    install_security(app)
    app.include_router(diagrams.router)
    return TestClient(app)


def test_every_registered_diagram_file_exists():
    assert diagrams.DIAGRAMS, "the tab needs at least one diagram"
    for name in diagrams.DIAGRAMS:
        assert (diagrams.DIAGRAM_DIR / f"{name}.html").is_file(), name


@pytest.mark.parametrize("name", list(diagrams.DIAGRAMS))
def test_diagram_served_sandboxed(diagram_client, name):
    r = diagram_client.get(f"/diagrams/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    csp = r.headers["Content-Security-Policy"]
    # The relaxation is only tolerable because of these: an opaque origin that cannot phone home.
    assert "sandbox allow-scripts" in csp
    assert "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    # The middleware's defaults still apply to the headers the route does not own.
    assert r.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("path", ["/diagrams/nope", "/diagrams/..%2Fmain", "/diagrams/overview.html"])
def test_unknown_diagram_is_404(diagram_client, path):
    assert diagram_client.get(path).status_code == 404


def test_dashboard_keeps_strict_csp_and_has_diagram_tab(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 60000.0))
    with TestClient(fastapi_app) as c:
        home = c.get("/")
    assert home.headers["X-Frame-Options"] == "DENY"
    assert "script-src" not in home.headers["Content-Security-Policy"]  # still default-src 'self'
    assert 'data-tab="diagrams"' in home.text
    assert 'data-tab-panel="diagrams"' in home.text
    for name in diagrams.DIAGRAMS:
        # ?theme=dark: the dashboard is dark-only and the sandboxed viewer cannot read a stored theme.
        assert f'href="/diagrams/{name}?theme=dark"' in home.text
