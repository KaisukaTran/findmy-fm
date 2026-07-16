"""Config-deadlock guard: an expectancy gate above its own mathematical ceiling.

Root cause of the 2026-07-16 silent halt: expectancy can never exceed
``scan_tp_pct − round_trip_cost``, so ``min_expectancy_pct = 3.0`` with ``scan_tp_pct = 3.0``
(ceiling 2.70%) skipped 100% of candidates across 25 scans / 12 hours with no alert.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import costengine, models, scanner
from app.config import settings
from app.data import candle_cache
from app.main import app

_DAY = 86_400_000


def _uptrend(n=60, start=100.0, vol=1e6):
    out, price = [], start
    for d in range(n):
        out.append({"ts": d * _DAY, "open": price, "high": price,
                    "low": price * 0.999, "close": price, "volume": vol})
        price *= 1.01
    return out


class _FakeProvider:
    def __init__(self):
        self._candles = {"BTC": _uptrend(), "ETH": _uptrend()}

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
    """Hermetic scan: never touches the live exchange (see run-ops — patch _provider_factory)."""
    fake = _FakeProvider()
    monkeypatch.setattr(scanner, "data_provider", lambda: fake)
    monkeypatch.setattr(scanner, "_provider_factory", lambda _xid: fake)
    candle_cache.clear()
    monkeypatch.setattr("app.market.get_current_prices",
                        lambda syms, force=False: fake.get_prices(syms))
    monkeypatch.setattr(settings, "watchlist", ["BTC", "ETH"])
    monkeypatch.setattr(settings, "scan_top_n", 0)
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)
    monkeypatch.setattr(settings, "auto_trade", False)
    monkeypatch.setattr(settings, "backtest_trial_spacing_days", 0.0)
    return fake


def test_expectancy_ceiling_is_tp_minus_round_trip_cost(monkeypatch):
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)
    # 3.0 TP − (2×0.1 + 2×0.05) = 2.70 — the exact number every candidate reported.
    assert costengine.expectancy_ceiling_pct(3.0) == pytest.approx(2.70)


def test_gate_unsatisfiable_only_above_the_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)
    # The shipped deadlock: min 3.0 > ceiling 2.70 → no coin can ever trade.
    assert costengine.expectancy_gate_unsatisfiable(3.0, 3.0)
    # Exactly at the ceiling is satisfiable (a perfect-win-rate coin clears it).
    assert not costengine.expectancy_gate_unsatisfiable(2.70, 3.0)
    # The approved fix, and the pre-reset posture (tp 4.0 → ceiling 3.70).
    assert not costengine.expectancy_gate_unsatisfiable(2.0, 3.0)
    assert not costengine.expectancy_gate_unsatisfiable(3.0, 4.0)


def test_shipped_defaults_are_coherent():
    """The defaults themselves were deadlocked (min_expectancy 3.0 vs a 2.70 ceiling), so a fresh
    instance — or a live promote — would never open a single session. Lock that shut."""
    from app.config import Settings

    d = Settings.model_fields
    tp = d["scan_tp_pct"].default
    min_e = d["min_expectancy_pct"].default
    cost = 2 * d["taker_fee_pct"].default + 2 * d["slippage_pct"].default
    assert min_e <= tp - cost, (
        f"default min_expectancy_pct={min_e} exceeds the ceiling {tp - cost:.2f} "
        f"(scan_tp {tp} − cost {cost:.2f}) → scanner can never trade"
    )


def test_endpoint_rejects_unsatisfiable_gate(monkeypatch):
    """POST must refuse a self-contradicting pair instead of silently halting the scanner."""
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)
    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 2.0)
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={"min_expectancy_pct": 3.0})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "2.70" in detail and "3.00" in detail
        # Rejected atomically — the live setting is untouched.
        assert settings.min_expectancy_pct == 2.0


def test_endpoint_rejects_tp_that_strands_the_current_gate(monkeypatch):
    """The deadlock arrived via the OTHER knob: lowering tp under a standing min_expectancy."""
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)
    monkeypatch.setattr(settings, "scan_tp_pct", 4.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 3.0)
    with TestClient(app) as c:
        assert c.post("/api/kss-settings", json={"scan_tp_pct": 3.0}).status_code == 400
        assert settings.scan_tp_pct == 4.0


def _audit_rows(db, action):
    return db.query(models.AuditLog).filter_by(action=action).all()


def test_scan_alarms_when_gate_is_unsatisfiable(db, scan_env, monkeypatch, caplog):
    """A deadlock persisted BEFORE this guard shipped still loads from runtime_config/.env —
    the scan must shout every cycle instead of skipping 100% of the universe in silence."""
    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 3.0)  # ceiling 2.70 → impossible

    with caplog.at_level("WARNING"):
        scanner.run_scan(db, mode="semi")

    rows = _audit_rows(db, "gate_unsatisfiable")
    assert len(rows) == 1, "config deadlock must be audited once per scan"
    detail = json.loads(rows[0].detail)
    assert detail["min_expectancy_pct"] == 3.0
    assert detail["expectancy_ceiling_pct"] == pytest.approx(2.70)
    assert detail["scan_tp_pct"] == 3.0
    assert any("gate" in r.message.lower() for r in caplog.records)
    # The real symptom the alarm explains: nothing can trade.
    assert db.query(models.Candidate).filter_by(decision="trade").count() == 0


def test_scan_is_silent_when_gate_is_satisfiable(db, scan_env, monkeypatch):
    """No false alarm on a coherent config — the approved 2.0 gate under a 2.70 ceiling."""
    monkeypatch.setattr(settings, "scan_tp_pct", 3.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 2.0)

    scanner.run_scan(db, mode="semi")

    assert _audit_rows(db, "gate_unsatisfiable") == []


def test_endpoint_accepts_both_knobs_moved_together(monkeypatch):
    """Lowering tp AND the gate in one call is coherent — must pass."""
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)
    monkeypatch.setattr(settings, "scan_tp_pct", 4.0)
    monkeypatch.setattr(settings, "min_expectancy_pct", 3.0)
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={"scan_tp_pct": 3.0, "min_expectancy_pct": 2.0})
        assert r.status_code == 200
        assert r.json()["scan_tp_pct"] == 3.0
        assert r.json()["min_expectancy_pct"] == 2.0
