"""candle_cache keys on (exchange, symbol, timeframe) and ignores ``limit``: an entry first filled
by a SHORT request (e.g. the chart of a 2-day session) used to be served to a later caller that
needs a LONGER window (an 8-day session's BTC comparison) — fresh hit, or tail-extended only, so
the long caller silently got a truncated series. ``need_from_ms`` makes such an entry a miss.
All offline."""

from __future__ import annotations

import pytest

from app.data import candle_cache
from app.data.providers import Candle

H = 3_600_000


@pytest.fixture(autouse=True)
def _clean():
    candle_cache.clear()
    yield
    candle_cache.clear()


class _Provider:
    """Serves ``limit`` hourly bars ending at bar 1000."""

    def __init__(self):
        self.full_calls = 0

    def get_ohlcv(self, symbol, timeframe, limit):
        self.full_calls += 1
        return [Candle(ts=i * H, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)
                for i in range(1000 - limit + 1, 1001)]


def test_short_entry_is_refetched_when_caller_needs_older_bars():
    prov = _Provider()
    short, _ = candle_cache.get_candles("binance", "BTC", "1h", 50, lambda _x: prov)
    assert short[0]["ts"] == 951 * H

    longer, hit = candle_cache.get_candles("binance", "BTC", "1h", 200, lambda _x: prov,
                                           need_from_ms=801 * H)
    assert hit is False and prov.full_calls == 2
    assert longer[0]["ts"] == 801 * H
    # the longer series now backs the cache for everyone
    again, hit = candle_cache.get_candles("binance", "BTC", "1h", 50, lambda _x: prov)
    assert hit is True and again[0]["ts"] == 801 * H


def test_covering_entry_is_still_a_hit():
    prov = _Provider()
    candle_cache.get_candles("binance", "BTC", "1h", 200, lambda _x: prov)
    _, hit = candle_cache.get_candles("binance", "BTC", "1h", 50, lambda _x: prov,
                                      need_from_ms=900 * H)
    assert hit is True and prov.full_calls == 1


def test_without_need_from_behaviour_is_unchanged():
    prov = _Provider()
    candle_cache.get_candles("binance", "BTC", "1h", 50, lambda _x: prov)
    _, hit = candle_cache.get_candles("binance", "BTC", "1h", 200, lambda _x: prov)
    assert hit is True and prov.full_calls == 1
