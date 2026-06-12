"""
S6 property tests for estimate_win_rate invariants.

hypothesis is NOT installed; tests use parametrized example-based fixtures over
a range of synthetic candle series to cover the 3 invariants:

  P1. win_rate + loss_rate + flat_rate == 100 (within rounding) when trials > 0
  P2. expectancy is bounded by [-(sl_pct + cost_pct), tp_pct - cost_pct] when sl_pct > 0
  P3. increasing trial spacing does NOT increase the trial count (monotonic non-increasing)

No network calls; no DB. `app/backtest.py` is read-only — only estimate_win_rate is called.
"""

from __future__ import annotations

import pytest

from app.backtest import estimate_win_rate

_DAY = 86_400_000


def candle(day: int, close: float, high: float | None = None, low: float | None = None,
           open_: float | None = None) -> dict:
    h = high if high is not None else close
    lo = low if low is not None else close
    o = open_ if open_ is not None else close
    return {"ts": day * _DAY, "open": o, "high": h, "low": lo, "close": close, "volume": 1.0}


# ---------------------------------------------------------------------------
# Synthetic candle series fixtures
# ---------------------------------------------------------------------------

def _uptrend(n: int = 60, start: float = 100.0, step: float = 0.005) -> list[dict]:
    """Steadily rising prices; most entries reach TP quickly."""
    bars = []
    p = start
    for d in range(n):
        bars.append(candle(d, p, high=p * (1 + step), low=p * 0.999))
        p *= (1 + step)
    return bars


def _downtrend(n: int = 60, start: float = 100.0, step: float = 0.008) -> list[dict]:
    """Steadily falling prices; entries hit deadline with negative pnl → losses."""
    bars = []
    p = start
    for d in range(n):
        bars.append(candle(d, p, high=p * 1.001, low=p * (1 - step)))
        p *= (1 - step)
    return bars


def _flat_drift(n: int = 60, start: float = 100.0, step: float = 0.001) -> list[dict]:
    """Gentle upward drift; price stays above entry but below TP → flat exits."""
    bars = []
    p = start
    for d in range(n):
        bars.append(candle(d, p, high=p * 1.002, low=p * 0.999))
        p *= (1 + step)
    return bars


def _mixed(n_each: int = 40) -> list[dict]:
    """Concatenate uptrend + downtrend + flat-drift so all three outcome types appear."""
    day = 0
    bars = []
    p = 100.0
    for _ in range(n_each):
        bars.append(candle(day, p, high=p * 1.006, low=p * 0.999))
        p *= 1.005
        day += 1
    for _ in range(n_each):
        bars.append(candle(day, p, high=p * 1.001, low=p * 0.992))
        p *= 0.994
        day += 1
    for _ in range(n_each):
        bars.append(candle(day, p, high=p * 1.002, low=p * 0.999))
        p *= 1.001
        day += 1
    return bars


# ---------------------------------------------------------------------------
# P1: win_rate + loss_rate + flat_rate == 100 when trials > 0
# ---------------------------------------------------------------------------

_P1_CASES = [
    ("uptrend",   _uptrend(),   2.0, 5, 3.0, 30, 0.0, 0.0, 0),
    ("downtrend", _downtrend(), 2.0, 5, 3.0, 30, 0.0, 0.0, 0),
    ("flat",      _flat_drift(), 2.0, 5, 3.0, 30, 0.0, 0.0, 0),
    ("mixed",     _mixed(),     2.0, 5, 3.0, 30, 0.0, 0.0, 0),
    # With SL + cost
    ("up+sl",     _uptrend(),   2.0, 5, 3.0, 30, 5.0, 0.3, 0),
    ("down+sl",   _downtrend(), 2.0, 5, 3.0, 30, 5.0, 0.3, 0),
    ("mixed+sl",  _mixed(),     2.0, 5, 3.0, 30, 5.0, 0.3, 0),
    # With walk-forward split
    ("mixed+split", _mixed(),   2.0, 5, 3.0, 30, 3.0, 0.2, 0.5),
    # Spacing > 0
    ("mixed+spacing", _mixed(), 2.0, 5, 3.0, 30, 0.0, 0.0, 3),
]


@pytest.mark.parametrize(
    "label,candles,distance_pct,max_waves,tp_pct,deadline_days,sl_pct,cost_pct,spacing_days",
    _P1_CASES,
    ids=[c[0] for c in _P1_CASES],
)
def test_p1_rates_sum_to_100(
    label, candles, distance_pct, max_waves, tp_pct, deadline_days, sl_pct, cost_pct, spacing_days
):
    """P1: win_rate + loss_rate + flat_rate == 100 (to 0.1 rounding) whenever trials > 0."""
    res = estimate_win_rate(
        candles, distance_pct, max_waves, tp_pct, deadline_days,
        sl_pct=sl_pct, cost_pct=cost_pct, spacing_days=spacing_days,
    )
    if res["trials"] == 0:
        pytest.skip(f"{label}: no complete trials (candles too short for this config)")
    total_rate = round(res["win_rate"] + res["loss_rate"] + res["flat_rate"], 1)
    assert total_rate == 100.0, (
        f"{label}: win={res['win_rate']} + loss={res['loss_rate']} + flat={res['flat_rate']}"
        f" = {total_rate}, expected 100"
    )
    # Also verify count consistency
    count_sum = res["wins"] + res["losses"] + res["flats"]
    assert count_sum == res["trials"], (
        f"{label}: wins+losses+flats={count_sum} != trials={res['trials']}"
    )


# ---------------------------------------------------------------------------
# P2: expectancy bounded by [-(sl_pct + cost_pct), tp_pct - cost_pct] when sl_pct > 0
# ---------------------------------------------------------------------------

_P2_CASES = [
    # (label, candles, distance_pct, max_waves, tp_pct, deadline_days, sl_pct, cost_pct)
    ("up_sl5",     _uptrend(),   2.0, 5, 3.0, 30, 5.0, 0.0),
    ("down_sl5",   _downtrend(), 2.0, 5, 3.0, 30, 5.0, 0.0),
    ("mixed_sl5",  _mixed(),     2.0, 5, 3.0, 30, 5.0, 0.0),
    ("up_sl10",    _uptrend(),   2.0, 5, 3.0, 30, 10.0, 0.3),
    ("down_sl10",  _downtrend(), 2.0, 5, 3.0, 30, 10.0, 0.3),
    ("mixed_sl10", _mixed(),     2.0, 5, 3.0, 30, 10.0, 0.3),
    ("flat_sl8",   _flat_drift(), 2.0, 5, 3.0, 30, 8.0, 0.15),
    # Tight TP — many deadline exits expected
    ("mixed_tp1",  _mixed(),     2.0, 5, 1.0, 30, 3.0, 0.2),
    # Large distance keeps waves shallow → more deadline exits
    ("mixed_d5",   _mixed(),     5.0, 5, 4.0, 30, 6.0, 0.1),
]


@pytest.mark.parametrize(
    "label,candles,distance_pct,max_waves,tp_pct,deadline_days,sl_pct,cost_pct",
    _P2_CASES,
    ids=[c[0] for c in _P2_CASES],
)
def test_p2_expectancy_bounded(
    label, candles, distance_pct, max_waves, tp_pct, deadline_days, sl_pct, cost_pct
):
    """P2: expectancy lies within [-(sl_pct+cost_pct), tp_pct-cost_pct] when sl_pct > 0.

    The lower bound is the worst-case realized pnl (full SL hit + round-trip cost).
    The upper bound is the best-case realized pnl (full TP hit minus cost).
    Deadline exits may produce pnl outside those bounds in theory (pnl can be worse
    than -sl if the position is deeply underwater at deadline), BUT once sl_pct > 0 the
    stop fires first, so the minimum realized pnl per trial is capped at -(sl+cost).
    Therefore the per-trial pnl is always >= -(sl+cost), which makes the expectancy
    (the mean) also >= -(sl+cost).  Upper bound tp-cost holds because TP is the only
    positive exit.
    """
    res = estimate_win_rate(
        candles, distance_pct, max_waves, tp_pct, deadline_days,
        sl_pct=sl_pct, cost_pct=cost_pct,
    )
    if res["trials"] == 0:
        pytest.skip(f"{label}: no complete trials")
    lower = -(sl_pct + cost_pct)
    upper = tp_pct - cost_pct
    exp = res["expectancy"]
    assert exp >= lower - 1e-6, (
        f"{label}: expectancy {exp} < lower bound {lower}"
    )
    assert exp <= upper + 1e-6, (
        f"{label}: expectancy {exp} > upper bound {upper}"
    )


# ---------------------------------------------------------------------------
# P3: increasing trial spacing does NOT increase the trial count (monotonic non-increasing)
# ---------------------------------------------------------------------------

_P3_SPACINGS = [0, 1, 3, 7, 14, 30]  # days

_P3_CANDLES = [
    ("uptrend",  _uptrend(n=120)),
    ("downtrend", _downtrend(n=120)),
    ("mixed",    _mixed(n_each=50)),
]


@pytest.mark.parametrize("label,candles", _P3_CANDLES, ids=[c[0] for c in _P3_CANDLES])
def test_p3_spacing_reduces_trials_monotonically(label, candles):
    """P3: larger spacing_days yields <= trial count of smaller spacing (non-increasing)."""
    trial_counts: list[int] = []
    for sp in _P3_SPACINGS:
        res = estimate_win_rate(
            candles, distance_pct=2.0, max_waves=5, tp_pct=3.0,
            deadline_days=30, spacing_days=float(sp),
        )
        trial_counts.append(res["trials"])

    for i in range(1, len(trial_counts)):
        assert trial_counts[i] <= trial_counts[i - 1], (
            f"{label}: spacing {_P3_SPACINGS[i]}d gave more trials ({trial_counts[i]}) "
            f"than spacing {_P3_SPACINGS[i-1]}d ({trial_counts[i-1]}) — expected non-increasing"
        )
