"""Tests for app.data.providers (ccxt wrapper) with a fake offline exchange."""

import threading
import time

import ccxt
import pytest

from app import execution
from app.config import settings
from app.data.providers import CcxtProvider, get_provider, reset_providers


class _FakeEx:
    _prices = {"BTC/USD": 65000.0, "ETH/USD": 3500.0}

    def fetch_ticker(self, pair):
        return {"last": self._prices[pair]}

    def fetch_ohlcv(self, pair, timeframe="1d", limit=200):
        base = 1_000_000_000_000
        return [[base + i * 86_400_000, 100 + i, 101 + i, 99 + i, 100 + i, 5.0] for i in range(limit)]

    def fetch_tickers(self):
        # Fix C: get_prices/top_symbols/all_symbols now share ONE fetch_tickers() call (no
        # `symbols` filter — matches real ccxt 4.0.5, whose fetch_tickers ignores it server
        # side), so this single fixture must carry both `last` (get_prices) and `quoteVolume`
        # (top_symbols/all_symbols).
        return {
            "BTC/USD": {"quoteVolume": 1e9, "last": self._prices["BTC/USD"]},
            "ETH/USD": {"quoteVolume": 5e8, "last": self._prices["ETH/USD"]},
            "DOGE/USDT": {"quoteVolume": 9e9, "last": 0.4},  # wrong quote -> excluded
        }

    def market(self, pair):
        return {"limits": {"amount": {"min": 0.0001, "max": 1000.0}, "cost": {"min": 5.0}},
                "precision": {"amount": 0.0001}}


def _provider():
    p = CcxtProvider("coinbase")  # ccxt.coinbase() constructs offline; no network until a fetch
    p._ex = _FakeEx()
    return p


def test_pair_uses_exchange_quote():
    assert _provider().pair("BTC") == "BTC/USD"


def test_get_prices():
    assert _provider().get_prices(["BTC", "ETH"]) == {"BTC": 65000.0, "ETH": 3500.0}


def test_get_ohlcv_shape():
    candles = _provider().get_ohlcv("BTC", limit=10)
    assert len(candles) == 10
    assert candles[0]["close"] == 100 and candles[9]["close"] == 109
    assert set(candles[0].keys()) == {"ts", "open", "high", "low", "close", "volume"}


def test_top_symbols_filters_quote_and_sorts():
    top = _provider().top_symbols(5)
    assert top == ["BTC", "ETH"]  # DOGE/USDT excluded for a USD-quote exchange


def test_all_symbols_volume_floor():
    p = _provider()
    assert p.all_symbols(0) == ["BTC", "ETH"]          # by volume desc, USD quote only
    assert p.all_symbols(6e8) == ["BTC"]               # ETH (5e8) filtered out by floor


def test_exchange_info():
    info = _provider().get_exchange_info("BTC")
    assert info["minQty"] == 0.0001 and info["minNotional"] == 5.0


def test_ccxt_provider_applies_the_configured_socket_timeout(monkeypatch):
    """ccxt.coinbase() constructs offline (no network) — the real client is safe to inspect
    directly. Without an explicit timeout a wedged socket hangs forever instead of raising."""
    monkeypatch.setattr(settings, "exchange_timeout_sec", 8.25)

    p = CcxtProvider("coinbase")

    assert p._ex.timeout == 8250, "ccxt timeout is milliseconds"


# --- P2 Fix B2/B3: the data path must recognize 429/418 and stop, not amplify ------------


@pytest.fixture(autouse=True)
def _clean_execution_state():
    execution.reset_client_cache()
    yield
    execution.reset_client_cache()


def _ban() -> Exception:
    exc = ccxt.DDoSProtection("binance 418 banned")
    exc.http_status_code = 418
    return exc


def _rate429() -> Exception:
    exc = ccxt.DDoSProtection("binance 429 too many")
    exc.http_status_code = 429
    return exc


class _RateLimitedTickersEx:
    """``fetch_tickers`` always raises a rate-classified error; counts calls."""

    def __init__(self, exc_factory):
        self.calls = 0
        self._exc_factory = exc_factory

    def fetch_tickers(self):
        self.calls += 1
        raise self._exc_factory()

    def fetch_ticker(self, pair):  # pragma: no cover — must never be reached on a batch 429/418
        raise AssertionError("must not fan out per-symbol on a rate-classified batch error")


def test_get_prices_rate_error_on_batch_returns_empty_without_fanout():
    fake = _RateLimitedTickersEx(_ban)
    p = CcxtProvider("coinbase")
    p._ex = fake

    result = p.get_prices(["BTC", "ETH"])

    assert result == {}
    assert fake.calls == 1, "a 429/418 on the batch must not amplify into per-symbol requests"
    assert execution.rate_hold_active() is True


class _NonRateBatchEx:
    """Batched ``fetch_tickers`` fails for a NON-rate reason; per-symbol fallback still works."""

    def __init__(self):
        self.batch_calls = 0
        self.ticker_calls: list[str] = []

    def fetch_tickers(self):
        self.batch_calls += 1
        raise TypeError("this exchange does not support batched tickers")

    def fetch_ticker(self, pair):
        self.ticker_calls.append(pair)
        return {"last": 100.0}


def test_get_prices_non_rate_error_still_falls_back_per_symbol():
    fake = _NonRateBatchEx()
    p = CcxtProvider("coinbase")
    p._ex = fake

    result = p.get_prices(["BTC", "ETH"])

    assert result == {"BTC": 100.0, "ETH": 100.0}
    assert fake.batch_calls == 1
    assert fake.ticker_calls == ["BTC/USD", "ETH/USD"]
    assert execution.rate_hold_active() is False


class _RateErrorMidFallbackEx:
    """Batch fails non-rate; the SECOND per-symbol fetch is rate-classified."""

    def __init__(self):
        self.batch_calls = 0
        self.ticker_calls: list[str] = []

    def fetch_tickers(self):
        self.batch_calls += 1
        raise TypeError("no batched tickers")

    def fetch_ticker(self, pair):
        self.ticker_calls.append(pair)
        if len(self.ticker_calls) == 2:
            raise _rate429()
        return {"last": 100.0}


def test_get_prices_rate_error_mid_fallback_aborts_the_loop():
    fake = _RateErrorMidFallbackEx()
    p = CcxtProvider("coinbase")
    p._ex = fake

    result = p.get_prices(["BTC", "ETH", "SOL"])

    assert result == {"BTC": 100.0}                     # only the first symbol resolved
    assert fake.ticker_calls == ["BTC/USD", "ETH/USD"]   # SOL never attempted
    assert execution.rate_hold_active() is True


class _RateLimitedOhlcvEx:
    def __init__(self, exc_factory=_rate429):
        self.calls = 0
        self._exc_factory = exc_factory

    def fetch_ohlcv(self, pair, timeframe="1d", limit=200):
        self.calls += 1
        raise self._exc_factory()

    def parse_timeframe(self, tf):
        return 86400


def test_get_ohlcv_notes_a_rate_classified_error_and_degrades_to_empty():
    fake = _RateLimitedOhlcvEx()
    p = CcxtProvider("coinbase")
    p._ex = fake

    result = p.get_ohlcv("BTC", limit=10)

    assert result == []
    assert fake.calls == 1
    assert execution.rate_hold_active() is True


def test_top_symbols_notes_a_rate_classified_error_and_degrades_to_empty():
    fake = _RateLimitedTickersEx(_rate429)
    p = CcxtProvider("coinbase")
    p._ex = fake

    assert p.top_symbols(5) == []
    assert execution.rate_hold_active() is True


def test_all_symbols_notes_a_rate_classified_error_and_degrades_to_empty():
    fake = _RateLimitedTickersEx(_ban)
    p = CcxtProvider("coinbase")
    p._ex = fake

    assert p.all_symbols(0) == []
    assert execution.rate_hold_active() is True


# --- P2 Fix C: one TTL cache of fetch_tickers, shared by get_prices/top_symbols/all_symbols --


def test_ticker_cache_is_shared_across_methods_within_the_ttl(monkeypatch):
    fake = _FakeEx()
    calls: list[int] = []
    orig_fetch_tickers = fake.fetch_tickers

    def _counted():
        calls.append(1)
        return orig_fetch_tickers()

    fake.fetch_tickers = _counted
    p = CcxtProvider("coinbase")
    p._ex = fake

    clock = [1_000.0]
    monkeypatch.setattr("app.data.providers.time.monotonic", lambda: clock[0])

    p.get_prices(["BTC"])
    p.top_symbols(5)
    p.all_symbols(0)

    assert len(calls) == 1, "get_prices/top_symbols/all_symbols must share one venue fetch"

    clock[0] += 61.0  # past the 60s TTL
    p.get_prices(["BTC"])

    assert len(calls) == 2, "a call after the TTL expires must fetch again"


def test_a_rate_classified_error_does_not_poison_the_ticker_cache(monkeypatch):
    """A good map, once cached, survives a later rate-classified failure — the caller gets
    the LAST GOOD map's age, not an empty result that overwrites it."""
    fake = _FakeEx()
    p = CcxtProvider("coinbase")
    p._ex = fake

    clock = [1_000.0]
    monkeypatch.setattr("app.data.providers.time.monotonic", lambda: clock[0])

    first = p.get_prices(["BTC", "ETH"])
    assert first == {"BTC": 65000.0, "ETH": 3500.0}

    # TTL expires; the next fetch_tickers() call fails with a rate-classified error.
    clock[0] += 61.0

    def _boom():
        raise _ban()

    p._ex.fetch_tickers = _boom

    second = p.get_prices(["BTC", "ETH"])
    assert second == {}  # this call itself degrades — no fan-out on a rate error

    # The cache attributes are untouched by the failed refresh (still the old good map/ts).
    assert p._tickers_cache is not None
    assert p._tickers_cache["BTC/USD"]["last"] == 65000.0


def test_forced_guard_fetch_pierces_the_ticker_ttl_cache(monkeypatch):
    """The 90s position-guard's force=True prices size hard-SL decisions: when the WS feed is
    down, a forced fetch must reach the venue, never the 60s shared ticker cache (P2 review
    hardening — staleness on the exit path is exit-adjacent gating)."""
    prov = CcxtProvider("binance")
    calls = {"n": 0}

    def fake_fetch_tickers():
        calls["n"] += 1
        return {"BTC/USDT": {"last": 100.0 + calls["n"], "quoteVolume": 1.0}}

    monkeypatch.setattr(prov._ex, "fetch_tickers", fake_fetch_tickers, raising=False)
    assert prov.get_prices(["BTC"]) == {"BTC": 101.0}          # cold: 1 venue call
    assert prov.get_prices(["BTC"]) == {"BTC": 101.0}          # warm cache: still 1
    assert calls["n"] == 1
    assert prov.get_prices(["BTC"], fresh=True) == {"BTC": 102.0}  # forced: pierces the TTL
    assert calls["n"] == 2


def test_an_empty_tickers_result_is_not_cached(monkeypatch):
    """A degenerate SUCCESS (fetch_tickers() -> {}) must not be cached for the full TTL — the
    guard was `self._tickers_cache is not None`, so an empty dict passed it and was then
    served (empty) for 60s. The next call within the TTL must retry the network; once a real
    map comes back, THAT is cached normally."""
    prov = CcxtProvider("binance")
    calls = {"n": 0}
    responses = [{}, {}, {"BTC/USDT": {"last": 100.0, "quoteVolume": 1.0}}]

    def fake_fetch_tickers():
        calls["n"] += 1
        return responses[calls["n"] - 1]

    monkeypatch.setattr(prov._ex, "fetch_tickers", fake_fetch_tickers, raising=False)

    assert prov.get_prices(["BTC"]) == {}
    assert calls["n"] == 1
    assert prov._tickers_cache is None, "an empty map must not be cached"

    assert prov.get_prices(["BTC"]) == {}
    assert calls["n"] == 2, "still within the TTL, but the empty result must not be served from cache"

    assert prov.get_prices(["BTC"]) == {"BTC": 100.0}
    assert calls["n"] == 3
    assert prov._tickers_cache is not None, "a real map is cached normally"

    # Now within the TTL of the GOOD map: served from cache, no further network call.
    assert prov.get_prices(["BTC"]) == {"BTC": 100.0}
    assert calls["n"] == 3


# --- 2026-09-03 hang hardening: thread-safety around the shared ccxt client ------------------
#
# ccxt's sync client wraps ONE requests.Session, which is NOT thread-safe: the scheduler's
# scan sweep (outside _work_lock) and the 90s guard's price fetch share the SAME CcxtProvider
# instance, so a per-instance lock must serialize their HTTP calls onto that client.


class _OverlapTrackingEx:
    """A fake ccxt client whose fetch_tickers records whether two calls ever ran concurrently."""

    def __init__(self, hold_sec: float = 0.05):
        self.hold_sec = hold_sec
        self._active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def fetch_tickers(self):
        with self._guard:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        time.sleep(self.hold_sec)
        with self._guard:
            self._active -= 1
        return {"BTC/USD": {"quoteVolume": 1.0, "last": 1.0}}


def test_provider_serializes_concurrent_calls_on_the_same_instance():
    """Two threads calling the same provider method must never overlap on the shared client."""
    p = CcxtProvider("coinbase")
    p._ex = _OverlapTrackingEx()

    threads = [
        threading.Thread(target=lambda: p.get_prices(["BTC"], fresh=True)) for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert p._ex.max_active == 1, "two threads called the shared ccxt client concurrently"


def test_execution_client_lock_and_provider_lock_do_not_block_each_other(monkeypatch):
    """An exit (execution._client_lock) must never wait behind a scan sweep (provider._lock) —
    they must be genuinely INDEPENDENT locks, not one shared module-level lock."""
    p = CcxtProvider("coinbase")
    p._ex = _OverlapTrackingEx(hold_sec=0.3)

    scan_thread = threading.Thread(target=lambda: p.get_prices(["BTC"], fresh=True))
    scan_thread.start()
    time.sleep(0.05)  # let the scan actually be inside the provider lock

    class _FastFakeEx:
        def cancel_order(self, order_id, pair):
            return None

    monkeypatch.setattr(execution, "_client", lambda: _FastFakeEx())
    start = time.monotonic()
    execution.cancel_live_order("BTC/USDT", "1")
    elapsed = time.monotonic() - start

    scan_thread.join()
    assert elapsed < 0.2, f"execution call blocked behind the provider's scan lock ({elapsed:.3f}s)"


def test_get_provider_and_execution_client_are_distinct_objects(monkeypatch):
    """The scan-side lock lives on the PROVIDER (public data, no key); orders go through the
    EXECUTION client (authenticated). If these were ever the SAME object, a lock protecting one
    would also serialize the other — exactly what task 2 must NOT do (a SELL queueing behind a
    100-symbol sweep). Real ccxt construction is offline/no-network (verified: `timeout` is
    just a constructor kwarg), so this needs no fake."""
    reset_providers()
    execution.reset_client_cache()
    monkeypatch.setattr(settings, "live_exchange", "binance")
    monkeypatch.setattr(settings, "live_api_key", "k")
    monkeypatch.setattr(settings, "live_api_secret", "s")
    monkeypatch.setattr(settings, "live_use_testnet", False)
    try:
        provider_ex = get_provider("binance")._ex
        exec_ex = execution._client()

        assert provider_ex is not exec_ex
        assert type(provider_ex).__module__ == type(exec_ex).__module__  # both real ccxt.binance
    finally:
        reset_providers()
        execution.reset_client_cache()
