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


def test_order_approve_flow(client):
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
