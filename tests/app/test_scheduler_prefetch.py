"""The scan's OHLCV prefetch must run OUTSIDE the scheduler _work_lock.

Guarantees the fix that keeps the 90s position-guard responsive: the slow, read-only
candle fetch is warmed before the lock is taken, so the guard (which needs the same
lock) is not blocked during a cold-cache scan. Pure unit tests — everything stubbed,
no network, no DB.
"""

from __future__ import annotations

from app import scanner, scheduler
from app.config import settings


class _DummyDB:
    def close(self):  # _cycle_once calls db.close() in finally
        pass


def _stub_session(monkeypatch):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _DummyDB())


def test_prefetch_runs_outside_lock_then_cycle_inside(monkeypatch):
    """prefetch is called with _work_lock NOT held; run_cycle with it held; prefetch first."""
    _stub_session(monkeypatch)
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(scanner, "prefetch_universe_candles",
                        lambda db: calls.append(("prefetch", scheduler._work_lock.locked())))
    monkeypatch.setattr(scheduler, "run_cycle",
                        lambda db: calls.append(("cycle", scheduler._work_lock.locked())))
    scheduler._cycle_once()
    assert calls == [("prefetch", False), ("cycle", True)]
    assert not scheduler._work_lock.locked()  # released afterwards


def test_prefetch_failure_is_non_fatal(monkeypatch):
    """A prefetch error must not propagate and must NOT skip the locked run_cycle."""
    _stub_session(monkeypatch)
    calls: list[str] = []

    def _boom(db):
        raise RuntimeError("exchange unreachable")

    monkeypatch.setattr(scanner, "prefetch_universe_candles", _boom)
    monkeypatch.setattr(scheduler, "run_cycle", lambda db: calls.append("cycle"))
    scheduler._cycle_once()  # must not raise
    assert calls == ["cycle"]
    assert not scheduler._work_lock.locked()


def test_prefetch_universe_candles_warms_matching_set(monkeypatch):
    """It warms exactly the exchange/universe/timeframe/limit that run_scan will request,
    and never touches the work lock."""
    monkeypatch.setattr(scanner, "data_provider", lambda: object())
    monkeypatch.setattr(scanner, "_universe", lambda db, provider: ["BTC", "ETH"])
    seen: dict = {}
    monkeypatch.setattr(scanner, "_prefetch_candles",
                        lambda ex, syms, tf, limit: seen.update(ex=ex, syms=syms, tf=tf, limit=limit))
    n = scanner.prefetch_universe_candles(db=None)
    assert n == 2
    assert seen["ex"] == settings.data_exchange
    assert seen["syms"] == ["BTC", "ETH"]
    assert seen["tf"] == settings.backtest_timeframe
    assert seen["limit"] == scanner._days_to_bars(
        settings.backtest_lookback_days, settings.backtest_timeframe)
    assert not scheduler._work_lock.locked()
