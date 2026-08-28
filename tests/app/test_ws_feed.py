"""Tests for app.data.ws_feed (pure parser) and its app.market integration.

No real network/websocket connection is opened — BinancePriceFeed.run() is never
invoked here; only the pure parser and the market-side cache/gating logic are exercised.
"""

import pytest

import app.market as market
from app.data.ws_feed import parse_mini_ticker


@pytest.fixture(autouse=True)
def _clear_market_state():
    market.clear_cache()
    market.unregister_ws_feed()
    yield
    market.clear_cache()
    market.unregister_ws_feed()


# ---------------------------------------------------------------------------
# parse_mini_ticker — pure
# ---------------------------------------------------------------------------


def test_parse_mini_ticker_array_mixed_quotes():
    payload = [
        {"s": "BTCUSDT", "c": "65000.5"},
        {"s": "ETHUSDT", "c": "3500.25"},
        {"s": "BTCEUR", "c": "60000.0"},  # different quote — ignored
        {"s": "SOLBUSD", "c": "150.0"},  # different quote — ignored
    ]
    out = parse_mini_ticker(payload, "USDT")
    assert out == {"BTC": 65000.5, "ETH": 3500.25}


def test_parse_mini_ticker_ignores_missing_or_zero_close():
    payload = [
        {"s": "BTCUSDT", "c": "0"},  # zero — ignored
        {"s": "ETHUSDT"},  # missing c — ignored
        {"s": "ADAUSDT", "c": "1.23"},
    ]
    out = parse_mini_ticker(payload, "USDT")
    assert out == {"ADA": 1.23}


def test_parse_mini_ticker_single_dict_payload():
    out = parse_mini_ticker({"s": "BTCUSDT", "c": "65000.0"}, "USDT")
    assert out == {"BTC": 65000.0}


def test_parse_mini_ticker_empty_base_ignored():
    # symbol exactly equal to the quote would yield an empty base — defensively dropped.
    out = parse_mini_ticker([{"s": "USDT", "c": "1.0"}], "USDT")
    assert out == {}


# ---------------------------------------------------------------------------
# market.py integration
# ---------------------------------------------------------------------------


class _FakeFeed:
    def __init__(self, fresh: bool):
        self._fresh = fresh

    def is_fresh(self, max_age: float) -> bool:
        return self._fresh


class _SpyProvider:
    def __init__(self):
        self.calls = 0

    def get_prices(self, symbols):
        self.calls += 1
        return dict.fromkeys(symbols, 999.0)


def test_fresh_ws_feed_skips_rest_on_force(monkeypatch):
    market.note_ws_prices({"BTC": 65000.0})
    market.register_ws_feed(_FakeFeed(fresh=True))
    spy = _SpyProvider()
    monkeypatch.setattr(market, "live_provider", lambda: spy)

    prices = market.get_current_prices(["BTC"], force=True)

    assert prices["BTC"] == 65000.0
    assert spy.calls == 0


def test_stale_ws_feed_falls_back_to_rest_on_force(monkeypatch):
    market.note_ws_prices({"BTC": 65000.0})
    market.register_ws_feed(_FakeFeed(fresh=False))
    spy = _SpyProvider()
    monkeypatch.setattr(market, "live_provider", lambda: spy)

    prices = market.get_current_prices(["BTC"], force=True)

    assert prices["BTC"] == 999.0
    assert spy.calls == 1


def test_paper_regression_no_ws_feed_force_calls_rest(monkeypatch):
    """The key paper-unchanged guarantee: with no WS feed registered (_ws_feed is None,
    the paper default), force=True must call REST exactly as it always has."""
    assert market._ws_feed is None
    spy = _SpyProvider()
    monkeypatch.setattr(market, "live_provider", lambda: spy)

    prices = market.get_current_prices(["BTC"], force=True)

    assert prices["BTC"] == 999.0
    assert spy.calls == 1


def test_note_ws_prices_warms_cache_for_non_forced_reads(monkeypatch):
    spy = _SpyProvider()
    monkeypatch.setattr(market, "live_provider", lambda: spy)

    market.note_ws_prices({"BTC": 65000.0})
    prices = market.get_current_prices(["BTC"], force=False)

    assert prices["BTC"] == 65000.0
    assert spy.calls == 0
