"""Live-readiness 1.7 — asking for more candles than the venue returns in one call.

Binance caps klines at 1000 per request and does NOT complain when you ask for more: it
silently returns 1000. On a daily timeframe that never mattered (a year is 365 bars), but a
5m timeframe wants 105,120 bars for the same year — so the app would have run its backtest on
3.5 days of data while every label said 365. The provider therefore pages, and the scanner
refuses to ask for an unbounded number of intraday bars in the first place.
"""

from __future__ import annotations

import pytest

from app.data.providers import CcxtProvider

_TF_MS = {"5m": 300_000, "1h": 3_600_000, "1d": 86_400_000}


class _FakeExchange:
    """A venue that caps every kline response at ``page`` candles, like Binance's 1000."""

    def __init__(self, available: int, page: int = 1000, tf_ms: int = 300_000,
                 last_ts: int = 1_700_000_000_000):
        self.page = page
        self.tf_ms = tf_ms
        # Oldest..newest, evenly spaced, close == ts so assertions can identify a bar.
        self.bars = [
            [last_ts - (available - 1 - i) * tf_ms, 1.0, 2.0, 0.5, float(i), 10.0]
            for i in range(available)
        ]
        self.calls: list[dict] = []

    # ccxt's own helpers, which the paging path uses to place its cursor.
    def parse_timeframe(self, timeframe):
        if timeframe not in _TF_MS:
            raise ValueError(f"unknown timeframe {timeframe}")
        return _TF_MS[timeframe] / 1000

    def milliseconds(self):
        return self.bars[-1][0] + self.tf_ms

    def fetch_ohlcv(self, pair, timeframe="1d", limit=500, since=None, params=None):
        self.calls.append({"pair": pair, "timeframe": timeframe, "limit": limit, "since": since})
        rows = self.bars if since is None else [b for b in self.bars if b[0] >= since]
        capped = min(limit or self.page, self.page)
        # Without `since` a venue returns the NEWEST slice; with one, the oldest after it.
        return rows[-capped:] if since is None else rows[:capped]


def _provider(ex) -> CcxtProvider:
    prov = CcxtProvider.__new__(CcxtProvider)  # no network in __init__
    prov.exchange_id = "binance"
    prov.quote = "USDT"
    prov._ex = ex
    return prov


def test_a_request_inside_one_page_still_makes_one_call():
    ex = _FakeExchange(available=5000)
    prov = _provider(ex)

    candles = prov.get_ohlcv("BTC", "5m", 800)

    assert len(candles) == 800
    assert len(ex.calls) == 1


def test_more_than_one_page_is_fetched_in_pages_and_joined():
    ex = _FakeExchange(available=5000)
    prov = _provider(ex)

    candles = prov.get_ohlcv("BTC", "5m", 2500)

    assert len(candles) == 2500, "a capped venue must be paged, not trusted with one big limit"
    assert len(ex.calls) > 1
    ts = [c["ts"] for c in candles]
    assert ts == sorted(ts), "pages must be joined oldest-first"
    assert len(set(ts)) == len(ts), "the page boundary bar must not be duplicated"
    assert ts[-1] == ex.bars[-1][0], "the newest bar must be included"


def test_paging_stops_at_the_start_of_history():
    ex = _FakeExchange(available=1200)
    prov = _provider(ex)

    candles = prov.get_ohlcv("BTC", "5m", 5000)

    assert len(candles) == 1200  # everything the venue has, no infinite loop
    assert len(ex.calls) <= 8


def test_paging_gives_up_rather_than_looping_forever():
    """A venue that keeps answering with the same bar must not spin."""

    class _Stuck(_FakeExchange):
        def fetch_ohlcv(self, pair, timeframe="1d", limit=500, since=None, params=None):
            self.calls.append({"since": since})
            return [self.bars[0]]

    ex = _Stuck(available=5000)
    prov = _provider(ex)

    candles = prov.get_ohlcv("BTC", "5m", 5000)

    assert len(ex.calls) < 50
    assert len(candles) <= 1


def test_a_failure_mid_page_keeps_what_was_already_fetched():
    class _Flaky(_FakeExchange):
        def fetch_ohlcv(self, pair, timeframe="1d", limit=500, since=None, params=None):
            if len(self.calls) >= 2:
                raise RuntimeError("venue down")
            return super().fetch_ohlcv(pair, timeframe, limit, since, params)

    ex = _Flaky(available=5000)
    prov = _provider(ex)

    candles = prov.get_ohlcv("BTC", "5m", 3000)

    assert 0 < len(candles) <= 2000  # partial history beats none
    ts = [c["ts"] for c in candles]
    assert ts == sorted(ts)


def test_an_unknown_timeframe_falls_back_to_a_single_call():
    """Without a bar duration we cannot walk the cursor — one call is the safe answer."""
    ex = _FakeExchange(available=5000)
    prov = _provider(ex)

    candles = prov.get_ohlcv("BTC", "7m", 2500)

    assert len(ex.calls) == 1
    assert len(candles) <= 1000


# --- the scanner must not ask for a year of 5m bars -------------------------


def test_intraday_lookback_is_capped(monkeypatch):
    from app import scanner
    from app.config import settings

    monkeypatch.setattr(settings, "intraday_max_bars", 3000)

    assert scanner._days_to_bars(365, "5m") == 3000
    assert scanner._days_to_bars(365, "1h") == 3000


def test_the_daily_timeframe_is_untouched_by_the_cap(monkeypatch):
    from app import scanner
    from app.config import settings

    monkeypatch.setattr(settings, "intraday_max_bars", 3000)

    assert scanner._days_to_bars(365, "1d") == 365
    assert scanner._days_to_bars(30, "1d") == 30


def test_the_cap_can_be_turned_off(monkeypatch):
    from app import scanner
    from app.config import settings

    monkeypatch.setattr(settings, "intraday_max_bars", 0)

    assert scanner._days_to_bars(365, "5m") == pytest.approx(105_120)
