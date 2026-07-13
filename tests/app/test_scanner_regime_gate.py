"""
Scanner wiring tests for the market-wide BTC regime gate (SHADOW-FIRST).

BTC (the regime reference symbol) and ETH (a normal tradeable candidate) are both in the
watchlist. BTC is given a synthetic DOWNTREND (or uptrend) series so the regime classifies
risk_off (or risk_on); ETH is always a clean uptrend so it clears every other gate and would
normally open a session — isolating what the regime gate itself does to NEW opens.
"""

from __future__ import annotations

import json

import pytest

from app import models, scanner
from app.config import settings
from app.data import candle_cache

_DAY = 86_400_000


def _uptrend(n=60, start=100.0, vol=1e6):
    out, price = [], start
    for d in range(n):
        out.append({"ts": d * _DAY, "open": price, "high": price,
                    "low": price * 0.999, "close": price, "volume": vol})
        price *= 1.01
    return out


def _downtrend(n=60, start=200.0, vol=1e6):
    out, price = [], start
    for d in range(n):
        out.append({"ts": d * _DAY, "open": price, "high": price,
                    "low": price * 0.999, "close": price, "volume": vol})
        price *= 0.99
    return out


class _FakeProvider:
    def __init__(self, btc_candles):
        self._candles = {"BTC": btc_candles, "ETH": _uptrend()}

    def get_ohlcv(self, symbol, timeframe="1d", limit=200):
        return self._candles.get(symbol, [])

    def top_symbols(self, n=10):
        return []

    def all_symbols(self, min_quote_volume=0.0):
        return ["BTC", "ETH"]

    def get_prices(self, symbols):
        return {s: self._candles[s][-1]["close"] for s in symbols if s in self._candles}

    def get_exchange_info(self, symbol):
        return {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def scan_env(monkeypatch):
    def _make(btc_candles):
        fake = _FakeProvider(btc_candles)
        monkeypatch.setattr(scanner, "data_provider", lambda: fake)
        monkeypatch.setattr(scanner, "_provider_factory", lambda _xid: fake)
        candle_cache.clear()
        monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                            lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0})
        monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
        monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 1.0))
        monkeypatch.setattr("app.market.get_current_prices",
                            lambda syms, force=False: fake.get_prices(syms))
        monkeypatch.setattr(settings, "watchlist", ["BTC", "ETH"])
        monkeypatch.setattr(settings, "scan_top_n", 0)
        monkeypatch.setattr(settings, "min_confidence", 0.0)
        monkeypatch.setattr(settings, "min_win_rate", 0.0)
        monkeypatch.setattr(settings, "backtest_trial_spacing_days", 0.0)
        monkeypatch.setattr(settings, "min_trials", 0)
        monkeypatch.setattr(settings, "min_expectancy_pct", -100.0)
        monkeypatch.setattr(settings, "auto_trade", False)
        monkeypatch.setattr(settings, "block_downtrend_adx", 0.0)
        # Keep the regime SMA windows small so the 60-bar synthetic series is plenty of history.
        monkeypatch.setattr(settings, "regime_sma_fast", 5)
        monkeypatch.setattr(settings, "regime_sma_slow", 10)
        monkeypatch.setattr(settings, "regime_hysteresis_pct", 2.0)
        return fake
    return _make


def _audit_actions(db):
    return [a.action for a in db.query(models.AuditLog).all()]


def _audit_rows(db, action):
    return db.query(models.AuditLog).filter_by(action=action).all()


def test_enforcing_risk_off_blocks_all_new_opens(db, scan_env, monkeypatch):
    scan_env(_downtrend())
    monkeypatch.setattr(settings, "regime_gate_enabled", True)
    monkeypatch.setattr(settings, "regime_gate_enforcing", True)

    scanner.run_scan(db, mode="semi")

    eth = db.query(models.Candidate).filter_by(symbol="ETH").one()
    assert eth.decision == "skip"
    assert eth.session_id is None
    assert "regime BTC risk_off" in (eth.reason or "")
    assert db.query(models.KssSession).filter_by(symbol="ETH").count() == 0

    actions = _audit_actions(db)
    assert "regime_state" in actions
    assert "regime_gate_blocked_opens" in actions
    blocked = _audit_rows(db, "regime_gate_blocked_opens")[0]
    detail = json.loads(blocked.detail)
    assert detail["count"] >= 1


def test_shadow_mode_risk_off_does_not_block_opens(db, scan_env, monkeypatch):
    scan_env(_downtrend())
    monkeypatch.setattr(settings, "regime_gate_enabled", True)
    monkeypatch.setattr(settings, "regime_gate_enforcing", False)  # shadow

    scanner.run_scan(db, mode="semi")

    eth = db.query(models.Candidate).filter_by(symbol="ETH").one()
    assert eth.decision == "trade"
    assert eth.session_id is not None
    assert db.query(models.KssSession).filter_by(symbol="ETH").count() == 1

    actions = _audit_actions(db)
    assert "regime_state" in actions
    assert "regime_gate_shadow_would_block" in actions
    assert "regime_gate_blocked_opens" not in actions


def test_risk_on_blocks_nothing_even_when_enforcing(db, scan_env, monkeypatch):
    scan_env(_uptrend())
    monkeypatch.setattr(settings, "regime_gate_enabled", True)
    monkeypatch.setattr(settings, "regime_gate_enforcing", True)

    scanner.run_scan(db, mode="semi")

    eth = db.query(models.Candidate).filter_by(symbol="ETH").one()
    assert eth.decision == "trade"
    assert eth.session_id is not None

    actions = _audit_actions(db)
    assert "regime_state" in actions
    assert "regime_gate_blocked_opens" not in actions
    assert "regime_gate_shadow_would_block" not in actions


def test_gate_disabled_no_regime_audit_at_all(db, scan_env, monkeypatch):
    scan_env(_downtrend())
    monkeypatch.setattr(settings, "regime_gate_enabled", False)  # default/off
    monkeypatch.setattr(settings, "regime_gate_enforcing", True)  # irrelevant while disabled

    scanner.run_scan(db, mode="semi")

    eth = db.query(models.Candidate).filter_by(symbol="ETH").one()
    assert eth.decision == "trade"
    assert eth.session_id is not None

    actions = _audit_actions(db)
    assert "regime_state" not in actions
    assert "regime_gate_blocked_opens" not in actions
    assert "regime_gate_shadow_would_block" not in actions
