"""End-to-end API + dashboard tests via FastAPI TestClient."""

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app.main import app as fastapi_app


@pytest.fixture
def client(monkeypatch):
    # Avoid live Binance calls in read views.
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 60000.0))
    with TestClient(fastapi_app) as c:
        yield c


def test_health_and_security_headers(client):
    r = client.get("/health")
    assert r.json()["status"] == "ok"
    home = client.get("/")
    assert home.status_code == 200 and "FINDMY-FM" in home.text
    assert home.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in home.headers["Content-Security-Policy"]


def test_dashboard_csp_is_explicit_and_never_unsafe(client):
    """Every directive the dashboard relies on is spelled out (not inherited from default-src),
    and nothing may ever loosen it: no 'unsafe-*', no wildcard, no remote host, no data: script.
    /diagrams carries its own sandboxed policy and is deliberately not covered here."""
    for path in ("/", "/health", "/partials/positions"):
        csp = client.get(path).headers["Content-Security-Policy"]
        directives = {d.split()[0]: d.split()[1:] for d in (x.strip() for x in csp.split(";")) if d}
        assert directives["default-src"] == ["'self'"]
        assert directives["script-src"] == ["'self'"]
        assert directives["connect-src"] == ["'self'"]
        assert directives["frame-ancestors"] == ["'none'"]
        assert directives["object-src"] == ["'none'"]
        assert directives["base-uri"] == ["'self'"]
        assert directives["form-action"] == ["'self'"]
        assert "'unsafe-" not in csp
        assert "*" not in csp and "http:" not in csp and "https:" not in csp and "//" not in csp


def test_order_approve_flow(client, monkeypatch):
    # Market at/below the BUY limit: a dashboard approval of a not-yet-marketable limit is refused.
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 49000.0))
    r = client.post("/api/orders", json={"symbol": "BTC", "side": "BUY", "quantity": 0.01, "price": 50000})
    assert r.status_code == 200
    oid = r.json()["order"]["id"]
    assert client.post(f"/api/pending/approve/{oid}").status_code == 200

    positions = client.get("/api/positions").json()
    assert any(p["symbol"] == "BTC" for p in positions)
    assert client.get("/api/summary").json()["total_trades"] >= 1


def test_kss_preview_and_create(client):
    pv = client.post("/api/kss/preview", json={
        "symbol": "BTC", "entry_price": 50000, "distance_pct": 2,
        "max_waves": 5, "isolated_fund": 1000, "tp_pct": 3,
    })
    assert pv.status_code == 200 and len(pv.json()["waves"]) == 5

    cr = client.post("/api/kss/sessions", json={
        "symbol": "BTC", "entry_price": 50000, "distance_pct": 2,
        "max_waves": 3, "isolated_fund": 100000, "tp_pct": 3,
    })
    assert cr.status_code == 200 and cr.json()["status"] == "pending"


def test_partials_render(client):
    for name in ("summary", "positions", "trades", "pending", "kss"):
        assert client.get(f"/partials/{name}").status_code == 200


def test_templates_accept_both_template_response_call_shapes():
    """starlette 1.x removed TemplateResponse(name, {"request": request, ...}); app.routes.templates
    keeps that legacy shape working AND takes the current (request, name, context) one."""
    from starlette.requests import Request

    from app.routes import templates

    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    ctx = {"request": req, "rows": [], "page": 1, "has_prev": False, "has_next": False,
           "sort": "", "dir": "asc"}
    legacy = templates.TemplateResponse("partials/positions.html", dict(ctx))
    current = templates.TemplateResponse(req, "partials/positions.html", dict(ctx))
    kw = templates.TemplateResponse("partials/positions.html", context=dict(ctx), status_code=201)
    assert legacy.body == current.body and b"No open positions." in legacy.body
    assert kw.status_code == 201

