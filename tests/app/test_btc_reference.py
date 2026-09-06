"""
Tests for the BTC reference fetch (2026-09-06).

`_btc_ref_return` reads `candle_map["BTC"]`, and the candle map only holds symbols that
survived `_trade_block_reason`. So whenever BTC itself was pre-blocked as a CANDIDATE — one
open BTC session under `max_sessions_per_symbol=1`, a BTC stop-cooldown, a pending BTC sell —
the benchmark vanished, `_btc_ret` became None, and both the relative-strength gate and the
strategy router silently turned themselves off. No audit row, no visible symptom: the book
recorded a scan that looked gated and was not.

A benchmark must not be a function of whether we happen to hold it. It is fetched only when
something reads it, so the older promise that a pre-blocked symbol costs zero OHLCV calls
survives in the default configuration where both consumers are off.
"""

from __future__ import annotations

import pytest
from test_scanner import scan_env  # noqa: F401  (fixture: fake provider + neutral gates)

from app import models, scanner
from app.config import settings
from app.models import SESSION_ACTIVE, KssSession


@pytest.fixture
def capture_fetch(monkeypatch):
    """Record the symbol list the scan actually asks the provider for."""
    seen: list[list[str]] = []
    original = scanner._prefetch_candles

    def spy(exchange_id, symbols, timeframe, limit):
        seen.append(list(symbols))
        return original(exchange_id, symbols, timeframe, limit)

    monkeypatch.setattr(scanner, "_prefetch_candles", spy)
    return seen


@pytest.mark.usefixtures("scan_env")
class TestBtcReferenceFetch:
    def test_btc_is_fetched_even_when_it_is_blocked_as_a_candidate(
            self, db, capture_fetch, monkeypatch):
        monkeypatch.setattr(settings, "watchlist", ["BTC", "ETH"])
        monkeypatch.setattr(settings, "max_sessions_per_symbol", 1)
        # The benchmark is fetched only when something reads it, so that a pre-blocked symbol
        # still costs zero OHLCV calls in the default configuration (both consumers off).
        monkeypatch.setattr(settings, "rel_strength_enabled", True)
        # An open BTC session pre-blocks BTC — the exact live shape that removed the benchmark.
        db.add(KssSession(symbol="BTC", entry_price=100.0, distance_pct=2.0, max_waves=3,
                          isolated_fund=100.0, tp_pct=3.0, timeout_x_min=60.0, gap_y_min=0.0,
                          status=SESSION_ACTIVE, current_wave=1, avg_price=100.0,
                          total_filled_qty=1.0, total_cost=100.0))
        db.commit()

        scanner.run_scan(db, mode="semi")

        assert capture_fetch, "the scan never reached the candle prefetch"
        assert "BTC" in capture_fetch[0], "the benchmark was dropped with the candidate"
        # ...and it is a reference only: BTC must still not be evaluated as a candidate.
        btc_rows = db.query(models.Candidate).filter_by(symbol="BTC").all()
        assert all(c.decision == "skip" for c in btc_rows)
        assert all("pre-blocked" in (c.reason or "") for c in btc_rows)

    def test_btc_is_not_fetched_twice_when_it_is_already_a_candidate(
            self, db, capture_fetch, monkeypatch):
        monkeypatch.setattr(settings, "watchlist", ["BTC", "ETH"])
        monkeypatch.setattr(settings, "rel_strength_enabled", True)
        scanner.run_scan(db, mode="semi")
        assert capture_fetch[0].count("BTC") == 1

    def test_no_reference_fetch_when_nothing_reads_the_benchmark(
            self, db, capture_fetch, monkeypatch):
        # The S3 promise: a pre-blocked symbol triggers ZERO OHLCV calls. With both consumers
        # off there is no benchmark to keep, so the blocked symbol must stay unfetched.
        monkeypatch.setattr(settings, "watchlist", ["BTC", "ETH"])
        monkeypatch.setattr(settings, "rel_strength_enabled", False)
        monkeypatch.setattr(settings, "strategy_router_enabled", False)
        monkeypatch.setattr(settings, "max_sessions_per_symbol", 1)
        db.add(KssSession(symbol="BTC", entry_price=100.0, distance_pct=2.0, max_waves=3,
                          isolated_fund=100.0, tp_pct=3.0, timeout_x_min=60.0, gap_y_min=0.0,
                          status=SESSION_ACTIVE, current_wave=1, avg_price=100.0,
                          total_filled_qty=1.0, total_cost=100.0))
        db.commit()

        scanner.run_scan(db, mode="semi")
        assert "BTC" not in capture_fetch[0]


@pytest.mark.usefixtures("scan_env")
def test_a_missing_benchmark_is_audited_instead_of_silently_disabling_the_gate(
        db, monkeypatch):
    monkeypatch.setattr(settings, "rel_strength_enabled", True)
    # Provider returns candles for everything except the benchmark.
    monkeypatch.setattr(scanner, "_btc_ref_return", lambda *a, **k: None)

    scanner.run_scan(db, mode="semi")

    rows = (db.query(models.AuditLog)
            .filter(models.AuditLog.action == "btc_reference_missing").all())
    assert rows, "the gate turned itself off without leaving a trace"
    assert rows[0].entity == "BTC"


@pytest.mark.usefixtures("scan_env")
def test_no_audit_noise_when_the_gate_is_off(db, monkeypatch):
    monkeypatch.setattr(settings, "rel_strength_enabled", False)
    monkeypatch.setattr(scanner, "_btc_ref_return", lambda *a, **k: None)

    scanner.run_scan(db, mode="semi")

    assert not (db.query(models.AuditLog)
                .filter(models.AuditLog.action == "btc_reference_missing").all())
