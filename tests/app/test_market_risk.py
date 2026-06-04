"""Tests for app.market (mocked Binance) and app.risk."""

from datetime import datetime

import pytest

import app.market as market
from app import models, risk


@pytest.fixture(autouse=True)
def _clear_market_cache():
    market.clear_cache()
    yield
    market.clear_cache()


class _FakeProvider:
    """Stands in for app.data.providers.CcxtProvider (counts price fetches)."""

    def __init__(self, calls):
        self._calls = calls

    def get_prices(self, symbols):
        self._calls["n"] += 1
        prices = {"BTC": 65000.0, "ETH": 3500.0}
        return {s: prices[s] for s in symbols if s in prices}

    def get_exchange_info(self, symbol):
        return {"symbol": symbol, "minQty": 0.00001, "maxQty": 9000.0,
                "stepSize": 0.00001, "minNotional": 10.0}


def test_get_current_prices_cached(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(market, "live_provider", lambda: _FakeProvider(calls))
    p1 = market.get_current_prices(["BTC", "ETH"])
    assert p1["BTC"] == 65000.0 and p1["ETH"] == 3500.0
    # second call within TTL serves from cache, no new exchange call
    p2 = market.get_current_prices(["BTC", "ETH"])
    assert p2 == p1
    assert calls["n"] == 1


def test_get_exchange_info_defaults_on_failure(monkeypatch):
    def boom():
        raise RuntimeError("offline")

    monkeypatch.setattr(market, "live_provider", boom)
    info = market.get_exchange_info("BTC")
    assert info["symbol"] == "BTC" and info["minQty"] == 0.00001


def test_calculate_order_qty(monkeypatch):
    monkeypatch.setattr(
        risk, "get_exchange_info", lambda s: {"minQty": 0.00001, "stepSize": 0.00001}
    )
    # 5 pips × 2.0 × 0.00001 = 0.0001
    assert abs(risk.calculate_order_qty("BTC", pips=5) - 0.0001) < 1e-9


def test_position_size_violation(db):
    # equity default 10000, max 10% => limit 1000 cost
    db.add(models.Position(symbol="BTC", quantity=0.02, avg_entry_price=60000.0, total_cost=1200.0))
    db.commit()
    v = risk.check_position_size("BTC", qty=0.0, price=0.0, db=db)
    assert v is not None and "exceeds max" in v


def test_daily_loss_violation(db):
    # max daily loss 5% of 10000 = 500
    db.add(models.Fill(symbol="BTC", side="SELL", quantity=1, price=1, realized_pnl=-600.0,
                       executed_at=datetime.utcnow()))
    db.commit()
    assert risk.check_daily_loss(db) is not None


def test_check_all_risks_pass(db):
    passed, violations = risk.check_all_risks("BTC", qty=0.0001, price=65000.0, db=db)
    assert passed is True and violations == []
