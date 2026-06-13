"""API tests for the scanner / audit / auto-trade endpoints."""

import pytest
from fastapi.testclient import TestClient

from app import scanner
from app.config import settings
from app.main import app as fastapi_app

_DAY = 86_400_000


def _uptrend(n=60, start=100.0, vol=1e6):
    out, price = [], start
    for d in range(n):
        out.append({"ts": d * _DAY, "open": price, "high": price,
                    "low": price * 0.999, "close": price, "volume": vol})
        price *= 1.01
    return out


class _FakeProvider:
    def get_ohlcv(self, symbol, timeframe="1d", limit=200):
        return _uptrend() if symbol == "BTC" else []

    def top_symbols(self, n=10):
        return []

    def all_symbols(self, min_quote_volume=0.0):
        return ["BTC"]

    def get_prices(self, symbols):
        return dict.fromkeys(symbols, 180.0)

    def get_exchange_info(self, symbol):
        return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(scanner, "data_provider", lambda: _FakeProvider())
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0})
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr("app.portfolio.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
    monkeypatch.setattr(settings, "watchlist", ["BTC"])
    monkeypatch.setattr(settings, "scan_top_n", 0)
    monkeypatch.setattr(settings, "min_confidence", 0.0)
    monkeypatch.setattr(settings, "min_win_rate", 0.0)
    # Neutralise the realistic-win-rate gates so this exercises the scan ENDPOINT, not the
    # statistical gates (every-bar trials, no min-trials floor, no expectancy floor).
    monkeypatch.setattr(settings, "backtest_trial_spacing_days", 0.0)
    monkeypatch.setattr(settings, "min_trials", 0)
    monkeypatch.setattr(settings, "min_expectancy_pct", -100.0)
    monkeypatch.setattr(settings, "auto_trade", False)
    with TestClient(fastapi_app) as c:
        yield c


def test_scan_endpoint_and_candidates(client):
    r = client.post("/api/scan")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "semi" and len(body["candidates"]) == 1

    cands = client.get("/api/candidates").json()
    assert cands and cands[0]["symbol"] == "BTC" and cands[0]["decision"] == "trade"

    audit = client.get("/api/audit").json()
    assert any(a["action"] == "scan_start" for a in audit)


def test_autotrade_toggle(client):
    assert client.get("/api/autotrade").json()["auto_trade"] is False
    r = client.post("/api/autotrade", json={"enabled": True})
    assert r.status_code == 200 and r.json()["auto_trade"] is True
    settings.auto_trade = False  # reset shared singleton for other tests


def test_scanner_and_audit_partials(client):
    client.post("/api/scan")
    assert client.get("/partials/scanner").status_code == 200
    assert client.get("/partials/audit").status_code == 200
