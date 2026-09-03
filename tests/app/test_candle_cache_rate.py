"""app.data.candle_cache's TAIL-FETCH path calls ``provider._ex.fetch_ohlcv`` directly (not
``provider.get_ohlcv``), so it bypasses that method's own rate-classified-error handling —
this is the ONE data-path location Fix B's provider-level noting does not reach on its own.
All offline — no network, no real keys."""

from __future__ import annotations

import ccxt
import pytest

from app import execution
from app.data import candle_cache
from app.data.providers import Candle


@pytest.fixture(autouse=True)
def _clean():
    candle_cache.clear()
    execution.reset_client_cache()
    yield
    candle_cache.clear()
    execution.reset_client_cache()


def _ban() -> Exception:
    exc = ccxt.DDoSProtection("binance 418 banned")
    exc.http_status_code = 418
    return exc


class _FakeEx:
    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    def fetch_ohlcv(self, pair, timeframe="1d", since=None):
        self.calls += 1
        raise self._exc


class _FakeProvider:
    def __init__(self, exc: Exception):
        self._ex = _FakeEx(exc)

    def pair(self, symbol: str) -> str:
        return f"{symbol}/USDT"

    def get_ohlcv(self, symbol, timeframe, limit):  # pragma: no cover — tail fetch bypasses this
        raise AssertionError("a tail fetch must not go through provider.get_ohlcv")


def _seed_stale_entry(symbol: str = "BTC", timeframe: str = "1d") -> tuple[str, str, str]:
    """Insert a STALE cache entry directly so get_candles takes the tail-fetch branch
    (since_ms is not None) rather than a cold/full fetch."""
    key = ("binance", symbol, timeframe)
    entry = candle_cache._CacheEntry(
        [Candle(ts=1_000, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]
    )
    entry.fetched_at -= 10**9  # force staleness regardless of the timeframe's TTL
    candle_cache._cache[key] = entry
    return key


def test_tail_fetch_rate_error_is_noted_and_degrades_to_the_stale_cache():
    _seed_stale_entry()
    fake = _FakeProvider(_ban())

    candles, hit = candle_cache.get_candles("binance", "BTC", "1d", 200, lambda _xid: fake)

    assert hit is False
    assert candles == [Candle(ts=1_000, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]
    assert fake._ex.calls == 1
    assert execution.rate_hold_active() is True


def test_tail_fetch_non_rate_error_degrades_without_noting_a_hold():
    _seed_stale_entry()
    fake = _FakeProvider(TypeError("boom"))

    candles, hit = candle_cache.get_candles("binance", "BTC", "1d", 200, lambda _xid: fake)

    assert hit is False
    assert candles == [Candle(ts=1_000, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]
    assert execution.rate_hold_active() is False


def test_provider_factory_failure_itself_is_tolerated_without_a_provider_bound():
    """provider_factory raising (before `provider` is ever assigned) must not crash the
    except-branch's rate-noting (it has nothing to read last_response_headers from)."""
    _seed_stale_entry()

    def _boom_factory(_xid):
        raise RuntimeError("no client available")

    candles, hit = candle_cache.get_candles("binance", "BTC", "1d", 200, _boom_factory)

    assert hit is False
    assert candles == [Candle(ts=1_000, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]
    assert execution.rate_hold_active() is False
