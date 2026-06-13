"""Tests for the TA layer: pure-Python indicators, the bundle builder, and tier fallback."""

from __future__ import annotations

import math

from app.data.providers import Candle
from app.ta import bundle, indicators


def _series(prices: list[float], vol: float = 1e6) -> list[Candle]:
    """Build candles from a close-price path (high/low straddle the close)."""
    return [
        Candle(ts=i * 86_400_000, open=p, high=p * 1.01, low=p * 0.99, close=p, volume=vol)
        for i, p in enumerate(prices)
    ]


def _uptrend(n: int = 120, start: float = 100.0, step: float = 1.01) -> list[Candle]:
    prices, p = [], start
    for _ in range(n):
        prices.append(p)
        p *= step
    return _series(prices)


def _downtrend(n: int = 120, start: float = 100.0, step: float = 0.99) -> list[Candle]:
    prices, p = [], start
    for _ in range(n):
        prices.append(p)
        p *= step
    return _series(prices)


# --- individual indicators ----------------------------------------------------


def test_macd_line_positive_in_uptrend_negative_in_downtrend():
    # The MACD line = fast EMA - slow EMA: positive when price trends up, negative down.
    # (The histogram measures the line's momentum, so its sign is not a trend proxy.)
    assert indicators.macd(_uptrend())["line"] > 0
    assert indicators.macd(_downtrend())["line"] < 0


def test_macd_neutral_when_too_short():
    m = indicators.macd(_series([100, 101, 102]))
    assert m == {"line": 0.0, "signal": 0.0, "hist": 0.0, "hist_pct": 0.0}


def test_bollinger_pct_b_bounds_and_neutral():
    # Steady uptrend rides the upper band -> %B high.
    assert indicators.bollinger(_uptrend())["pct_b"] > 0.5
    # Too short -> neutral 0.5, zero width.
    short = indicators.bollinger(_series([100, 101]))
    assert short == {"pct_b": 0.5, "bandwidth": 0.0}


def test_atr_pct_nonnegative_and_zero_when_short():
    assert indicators.atr_pct(_uptrend()) > 0
    assert indicators.atr_pct(_series([100, 101])) == 0.0


def test_adx_strong_in_trend_and_direction():
    up = indicators.adx(_uptrend())
    assert up["adx"] > 20  # a clean trend is strong
    assert up["plus_di"] > up["minus_di"]
    down = indicators.adx(_downtrend())
    assert down["minus_di"] > down["plus_di"]


def test_supertrend_direction():
    assert indicators.supertrend(_uptrend()) == "up"
    assert indicators.supertrend(_downtrend()) == "down"
    assert indicators.supertrend(_series([100, 101])) == "flat"


def test_support_resistance_distances_signed_correctly():
    # In an uptrend the last price is the high -> no resistance above (uses envelope),
    # and support sits well below.
    sr = indicators.support_resistance(_uptrend())
    assert sr["sup_dist_pct"] > 0
    assert sr["res_dist_pct"] >= 0


def test_volume_trend_obv_follows_price():
    assert indicators.volume_trend(_uptrend())["obv_up"] is True
    assert indicators.volume_trend(_downtrend())["obv_up"] is False


def test_htf_trend():
    assert indicators.htf_trend(_uptrend()) == "up"
    assert indicators.htf_trend(_downtrend()) == "down"
    assert indicators.htf_trend(_series([1, 2, 3])) == "flat"


# --- bundle -------------------------------------------------------------------


def test_bundle_shape_is_compact_and_rounded():
    b = bundle.build(_uptrend())
    # Compact keys present, values are plain JSON-friendly scalars.
    for key in ("rsi", "macd_h", "bb_pct", "adx", "atr_pct", "st", "htf", "vtrend"):
        assert key in b
    for v in b.values():
        assert isinstance(v, (int, float, str, bool))
        if isinstance(v, float):
            # rounded to <= 2 decimals (token budget)
            assert math.isclose(v, round(v, 2), abs_tol=1e-9)


def test_bundle_neutral_on_empty_candles_never_raises():
    b = bundle.build([])
    assert isinstance(b, dict) and b  # neutral, non-empty, no exception


def test_tier2_lib_overlays_when_enabled(monkeypatch):
    """Tier 2 (pandas-ta) overlays a few keys when enabled; skipped if the lib is absent."""
    pytest = __import__("pytest")
    pytest.importorskip("pandas_ta")
    from app.config import settings

    monkeypatch.setattr(settings, "ta_lib_enabled", True)
    b = bundle.build(_uptrend())
    assert b["adx"] > 0 and 0 <= b["rsi"] <= 100  # library values merged, still sane


def test_tier2_fails_open_to_tier1(monkeypatch):
    """A broken Tier 2 import must not break the bundle — it falls back to Tier 1."""
    from app.config import settings
    from app.ta import lib

    monkeypatch.setattr(settings, "ta_lib_enabled", True)
    monkeypatch.setattr(lib, "enrich", lambda candles: (_ for _ in ()).throw(RuntimeError("boom")))
    b = bundle.build(_uptrend())
    assert b["st"] == "up"  # Tier 1 still produced a full bundle
