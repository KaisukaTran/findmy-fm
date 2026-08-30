"""
Phase 0 regression tests for the stepSize misread (docs/capital-scaling-2026-08-23.md §2.2).

``ccxt.binance().precisionMode == DECIMAL_PLACES`` (2) means ``market['precision']['amount']``
is a COUNT OF DECIMALS (e.g. BNB -> 3, meaning step 0.001), not a step. The old code used it
directly as ``stepSize`` — for any coin above ~$17, ``pyramid.py``'s ``round(raw_qty / step)``
then collapsed the wave to ``minQty`` dust. These tests cover every ``precisionMode`` with a
fake (offline) ccxt market dict, plus one opt-in network test against live Binance filters.
"""

from __future__ import annotations

import os

import ccxt
import pytest

from app.data.providers import CcxtProvider, _step_size_from_market


def _market(precision_amount, filters=None, min_qty=None):
    m = {"precision": {"amount": precision_amount}, "limits": {"amount": {}, "cost": {}}}
    if filters is not None:
        m["info"] = {"filters": filters}
    if min_qty is not None:
        m["limits"]["amount"]["min"] = min_qty
    return m


# --- _step_size_from_market: one case per precision mode -----------------------------------


def test_decimal_places_prefers_lot_size_filter_when_present():
    """DECIMAL_PLACES with a real LOT_SIZE filter -> use the filter's stepSize verbatim."""
    filters = [{"filterType": "LOT_SIZE", "stepSize": "0.00100000", "minQty": "0.00100000"}]
    market = _market(3, filters=filters)  # BNB-shaped: precision.amount=3 (decimals), NOT a step
    step = _step_size_from_market(ccxt.DECIMAL_PLACES, market)
    assert step == pytest.approx(0.001)


def test_decimal_places_falls_back_to_ten_pow_minus_precision_without_filter():
    """DECIMAL_PLACES with no LOT_SIZE filter -> 10 ** -precision (the correct interpretation)."""
    market = _market(3)  # no "info" key at all -> no filters to read
    step = _step_size_from_market(ccxt.DECIMAL_PLACES, market)
    assert step == pytest.approx(0.001)


def test_tick_size_mode_uses_precision_value_as_is():
    """TICK_SIZE mode: precision.amount already IS the step (e.g. OKX-style)."""
    market = _market(0.0001)
    step = _step_size_from_market(ccxt.TICK_SIZE, market)
    assert step == pytest.approx(0.0001)


def test_tick_size_mode_prefers_lot_size_filter_when_present():
    filters = [{"filterType": "LOT_SIZE", "stepSize": "0.5"}]
    market = _market(0.0001, filters=filters)
    step = _step_size_from_market(ccxt.TICK_SIZE, market)
    assert step == pytest.approx(0.5)


def test_significant_digits_mode_falls_back_to_limits_min():
    """SIGNIFICANT_DIGITS: precision.amount is neither a step nor a safely-convertible
    decimal count -> fall back to limits.amount.min rather than guess."""
    market = _market(5, min_qty=0.01)
    step = _step_size_from_market(ccxt.SIGNIFICANT_DIGITS, market)
    assert step == pytest.approx(0.01)


def test_malformed_market_falls_back_to_default():
    """No usable filter, no precision, no limits.amount.min -> the documented default."""
    market = {"precision": {}, "limits": {}}
    step = _step_size_from_market(ccxt.DECIMAL_PLACES, market)
    assert step == 0.00001


def test_unknown_precision_mode_with_no_limits_falls_back_to_default():
    market = _market(None)
    step = _step_size_from_market(None, market)
    assert step == 0.00001


def test_malformed_lot_size_filter_falls_through_to_precision():
    """A LOT_SIZE entry present but with an unparseable stepSize must not crash — fall
    through to the precision-based derivation instead."""
    filters = [{"filterType": "LOT_SIZE", "stepSize": "not-a-number"}]
    market = _market(3, filters=filters)
    step = _step_size_from_market(ccxt.DECIMAL_PLACES, market)
    assert step == pytest.approx(0.001)


# --- CcxtProvider.get_exchange_info integration ---------------------------------------------


class _FakeEx:
    """Offline stand-in for a ccxt exchange instance."""

    def __init__(self, precision_mode, market):
        self.precisionMode = precision_mode
        self._market = market

    def market(self, pair):
        return self._market


def _provider_with(precision_mode, market):
    p = CcxtProvider("binance")
    p._ex = _FakeEx(precision_mode, market)
    return p


def test_get_exchange_info_uses_lot_size_filter():
    filters = [{"filterType": "LOT_SIZE", "stepSize": "0.00100000", "minQty": "0.00100000"}]
    market = _market(3, filters=filters, min_qty=0.001)
    market["limits"]["cost"]["min"] = 5.0
    info = _provider_with(ccxt.DECIMAL_PLACES, market).get_exchange_info("BNB")
    assert info["stepSize"] == pytest.approx(0.001)
    assert info["minQty"] == pytest.approx(0.001)
    assert info["minNotional"] == pytest.approx(5.0)


# --- Regression: the old bug (integer decimal-count used AS a step) cannot return ----------


def test_integer_precision_never_produces_a_whole_coin_step():
    """The old code did ``stepSize = market['precision']['amount']`` directly. For any market
    whose precision.amount is an integer >= 1 (Binance DECIMAL_PLACES shorthand for BNB=3,
    BTC=5, ...), a real Binance stepSize is NEVER a whole-coin quantity (3 or 5 whole coins is
    not a valid lot size on any Binance symbol) — so the derived step must always be < 1."""
    for precision in (1, 2, 3, 4, 5, 8):
        market = _market(precision)  # no LOT_SIZE filter -> exercises the fallback path
        step = _step_size_from_market(ccxt.DECIMAL_PLACES, market)
        assert step < 1, f"precision={precision} produced a whole-coin step {step} (old bug)"

        info = _provider_with(ccxt.DECIMAL_PLACES, market).get_exchange_info("X")
        assert info["stepSize"] < 1, f"precision={precision} -> get_exchange_info stepSize {info['stepSize']} >= 1"


def test_get_exchange_info_exception_path_returns_default_unchanged():
    """The except-fallback shape must be untouched by the stepSize fix."""
    class _BrokenEx:
        precisionMode = ccxt.DECIMAL_PLACES

        def market(self, pair):
            raise RuntimeError("network down")

    p = CcxtProvider("binance")
    p._ex = _BrokenEx()
    info = p.get_exchange_info("BTC")
    assert info == {
        "symbol": "BTC", "minQty": 0.00001, "maxQty": 10000.0,
        "stepSize": 0.00001, "minNotional": 10.0,
    }


# --- Opt-in network test: derived step vs live Binance LOT_SIZE ----------------------------

_RUN_NETWORK = os.environ.get("FINDMY_RUN_NETWORK_TESTS") == "1"


@pytest.mark.skipif(not _RUN_NETWORK, reason="set FINDMY_RUN_NETWORK_TESTS=1 to hit live Binance")
def test_derived_step_matches_live_binance_lot_size():
    """Live check (opt-in, not part of the default suite): the derived stepSize must equal
    the exchange's own LOT_SIZE.stepSize for a spread of expensive/coarse-precision symbols."""
    provider = CcxtProvider("binance")
    provider._ex.load_markets()
    symbols = ["BNB", "WBTC", "ZEC", "BCH", "PAXG", "BTC", "KLAY", "RVN"]
    for symbol in symbols:
        market = provider._ex.market(provider.pair(symbol))
        filters = {f.get("filterType"): f for f in (market.get("info") or {}).get("filters", [])}
        true_step = float(filters["LOT_SIZE"]["stepSize"])
        info = provider.get_exchange_info(symbol)
        assert info["stepSize"] == pytest.approx(true_step), (
            f"{symbol}: derived {info['stepSize']} != live LOT_SIZE.stepSize {true_step}"
        )
