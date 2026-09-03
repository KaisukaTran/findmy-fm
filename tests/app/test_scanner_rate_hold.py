"""Fix A (per-thread provider caching in scanner._provider_factory) and Fix B4 (the
scan-sweep rate-hold circuit: prefetch_universe_candles skips under an active hold, and
_prefetch_candles's ThreadPoolExecutor sweep aborts its remaining misses on the first
rate-classified failure). All offline — no network, no real keys."""

from __future__ import annotations

import threading

import ccxt
import pytest

from app import execution, models, scanner
from app.config import settings
from app.data import candle_cache


@pytest.fixture(autouse=True)
def _clean():
    scanner.reset_provider_factory_cache()
    scanner.reset_scan_fetch_pool()
    candle_cache.clear()
    execution.reset_client_cache()
    yield
    scanner.reset_provider_factory_cache()
    scanner.reset_scan_fetch_pool()
    candle_cache.clear()
    execution.reset_client_cache()


def _ban() -> Exception:
    exc = ccxt.DDoSProtection("binance 418 banned")
    exc.http_status_code = 418
    return exc


# --- Fix A: one ccxt client per (exchange_id, worker thread) -----------------------------


class _CountingProvider:
    """Stand-in for CcxtProvider that counts constructions instead of touching ccxt."""

    instances = 0

    def __init__(self, exchange_id: str):
        type(self).instances += 1
        self.exchange_id = exchange_id


def test_provider_factory_reuses_one_client_on_the_same_thread(monkeypatch):
    monkeypatch.setattr(scanner, "CcxtProvider", _CountingProvider)
    _CountingProvider.instances = 0

    a = scanner._provider_factory("binance")
    b = scanner._provider_factory("binance")
    c = scanner._provider_factory("binance")

    assert a is b is c
    assert _CountingProvider.instances == 1


def test_provider_factory_caches_per_exchange_id_too(monkeypatch):
    monkeypatch.setattr(scanner, "CcxtProvider", _CountingProvider)
    _CountingProvider.instances = 0

    binance = scanner._provider_factory("binance")
    coinbase = scanner._provider_factory("coinbase")
    binance_again = scanner._provider_factory("binance")

    assert binance is binance_again
    assert binance is not coinbase
    assert _CountingProvider.instances == 2


def test_provider_factory_gives_two_threads_at_most_two_constructions(monkeypatch):
    monkeypatch.setattr(scanner, "CcxtProvider", _CountingProvider)
    _CountingProvider.instances = 0

    same_thread_results: list[bool] = []

    def _worker():
        first = scanner._provider_factory("binance")
        # A second call on the SAME thread must reuse it, not construct another.
        second = scanner._provider_factory("binance")
        same_thread_results.append(first is second)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert same_thread_results == [True, True]
    assert _CountingProvider.instances <= 2, "at most one construction per thread"


# --- item 1: the fetch pool (and its workers' provider caches) survive across CALLS ------


def test_prefetch_candles_reuses_one_module_level_pool_across_calls(monkeypatch):
    """`_prefetch_candles` used to build (and then destroy, via `with ThreadPoolExecutor(...)
    as pool:`) a BRAND NEW pool on every call — so the worker threads (and their
    thread-local provider caches) died with it. This must go red on that per-call-pool
    code: only ONE ThreadPoolExecutor may ever be constructed across two separate calls."""
    scanner.reset_scan_fetch_pool()
    construct_calls: list[int] = []
    real_cls = scanner.ThreadPoolExecutor

    class _SpyPool(real_cls):
        def __init__(self, *a, **k):
            construct_calls.append(1)
            super().__init__(*a, **k)

    monkeypatch.setattr(scanner, "ThreadPoolExecutor", _SpyPool)
    monkeypatch.setattr(candle_cache, "get_candles",
                        lambda *a, **k: ([{"ts": 1, "open": 1, "high": 1, "low": 1,
                                          "close": 1, "volume": 1}], False))

    scanner._prefetch_candles("binance", ["BTC", "ETH"], "1d", 200)
    scanner._prefetch_candles("binance", ["SOL", "XRP"], "1d", 200)

    assert len(construct_calls) == 1, "the pool must be built once, not once per call"


def test_two_prefetch_calls_construct_at_most_scan_fetch_workers_providers_total(monkeypatch):
    """The whole point of the module-level pool: the SAME worker threads (and their
    thread-local `_provider_factory` caches) serve every call this process ever makes, so
    the TOTAL CcxtProvider construction count across two calls is bounded by the worker
    count, not by (workers × number of calls)."""
    monkeypatch.setattr(settings, "scan_fetch_workers", 2)
    scanner.reset_scan_fetch_pool()
    monkeypatch.setattr(scanner, "CcxtProvider", _CountingProvider)
    _CountingProvider.instances = 0
    candle_cache.clear()

    scanner._prefetch_candles("binance", ["BTC1", "ETH1", "SOL1"], "1d", 200)
    scanner._prefetch_candles("binance", ["BTC2", "ETH2", "SOL2"], "1d", 200)

    assert _CountingProvider.instances <= 2, (
        "at most `scan_fetch_workers` (2) CcxtProvider instances total across BOTH calls — "
        "a per-call pool would cold-build up to 2 more on the second call"
    )


# --- Fix B4: prefetch_universe_candles skips the WHOLE sweep under an active hold --------


def test_prefetch_universe_candles_skips_the_whole_sweep_under_an_active_hold(db, monkeypatch):
    reached: list[str] = []
    monkeypatch.setattr(scanner, "data_provider", lambda: object())
    monkeypatch.setattr(scanner, "_universe",
                        lambda db, provider: reached.append("universe") or ["BTC"])
    monkeypatch.setattr(scanner, "_prefetch_candles",
                        lambda *a, **k: reached.append("fetch") or {})

    execution.note_rate_error(_ban())

    n = scanner.prefetch_universe_candles(db)

    assert n == 0
    assert reached == [], "neither _universe() nor _prefetch_candles() should run under a hold"
    rows = db.query(models.AuditLog).filter_by(action="scan_rate_hold").all()
    assert len(rows) == 1


def test_prefetch_universe_candles_runs_normally_without_a_hold(db, monkeypatch):
    monkeypatch.setattr(scanner, "data_provider", lambda: object())
    monkeypatch.setattr(scanner, "_universe", lambda db, provider: ["BTC", "ETH"])
    monkeypatch.setattr(scanner, "_prefetch_candles", lambda *a, **k: ({}, False))

    n = scanner.prefetch_universe_candles(db)

    assert n == 2
    assert db.query(models.AuditLog).filter_by(action="scan_rate_hold").count() == 0


# --- Fix B4: _prefetch_candles skips ALL misses when a hold is already active ------------


def test_prefetch_candles_skips_all_misses_when_a_hold_is_already_active(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(candle_cache, "get_candles",
                        lambda *a, **k: calls.append(a[1]) or ([], False))

    execution.note_rate_error(_ban())

    result, aborted = scanner._prefetch_candles("binance", ["BTC", "ETH"], "1d", 200)

    assert calls == [], "no network fetch should be attempted while a hold is active"
    assert result == {}
    assert aborted is False, "a hold already active BEFORE the sweep started is not a mid-sweep abort"


# --- Fix B4: the ThreadPoolExecutor sweep's abort flag ------------------------------------


def test_prefetch_candles_worker_abort_stops_remaining_symbols_on_first_rate_error(monkeypatch):
    monkeypatch.setattr(settings, "scan_fetch_workers", 1)  # deterministic: one worker, in order
    calls: list[str] = []

    def _fake_get_candles(exchange_id, sym, timeframe, limit, factory):
        calls.append(sym)
        if len(calls) == 1:
            execution.note_rate_error(_ban())  # simulate what a real 418 would do
            return [], False
        return [{"ts": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}], False

    monkeypatch.setattr(candle_cache, "get_candles", _fake_get_candles)

    result, aborted = scanner._prefetch_candles("binance", ["BTC", "ETH", "SOL", "XRP"], "1d", 200)

    assert calls == ["BTC"], "the abort flag must stop the remaining symbols from being tried"
    for sym in ("BTC", "ETH", "SOL", "XRP"):
        assert result[sym] == ([], False)
    assert aborted is True, "a mid-sweep rate hold must be reported back to the caller"


# --- item 5: a mid-sweep abort must say what it is (not surface as skipped_thin_data) -----


def test_prefetch_universe_candles_writes_one_sweep_abort_row_on_a_mid_sweep_abort(db, monkeypatch):
    monkeypatch.setattr(scanner, "data_provider", lambda: object())
    monkeypatch.setattr(scanner, "_universe", lambda db, provider: ["BTC", "ETH"])
    monkeypatch.setattr(scanner, "_prefetch_candles",
                        lambda *a, **k: ({"BTC": ([], False)}, True))

    n = scanner.prefetch_universe_candles(db)

    assert n == 2
    rows = db.query(models.AuditLog).filter_by(action="scan_rate_hold", entity="sweep_abort").all()
    assert len(rows) == 1


def test_prefetch_universe_candles_writes_no_sweep_abort_row_when_not_aborted(db, monkeypatch):
    monkeypatch.setattr(scanner, "data_provider", lambda: object())
    monkeypatch.setattr(scanner, "_universe", lambda db, provider: ["BTC", "ETH"])
    monkeypatch.setattr(scanner, "_prefetch_candles", lambda *a, **k: ({}, False))

    scanner.prefetch_universe_candles(db)

    rows = db.query(models.AuditLog).filter_by(action="scan_rate_hold", entity="sweep_abort").all()
    assert rows == []
