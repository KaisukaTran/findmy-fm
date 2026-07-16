"""Regression: a wave must never collapse to a dust order.

The 2026-07-16 DASH bug: a coarse/glitchy exchange stepSize made `round(raw_qty / step)`
land on 0 for every wave, and `max(qty, minQty)` then floored each to the tiny minQty — so
DASH deployed 0.001 units (~$0.03) instead of the intended $15, three waves running
([0.001, 0.001, 0.001]). The session held essentially nothing and could never take a
meaningful profit. The fix: a positive target never rounds down to a dust order — it snaps
up to at least one whole lot (the smallest tradeable unit).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kss.pyramid import PyramidSession

# DASH-shaped filter: minQty far finer than a coarse (glitchy) stepSize — the exact combo
# that produced the dust position. Real DASH stepSize is 0.001; the coarse value here stands
# in for the transient exchange-info glitch seen at startup.
_DASH_BAD = {"minQty": 0.001, "stepSize": 1.0, "maxQty": 10000.0}
_DASH_OK = {"minQty": 0.001, "stepSize": 0.001, "maxQty": 10000.0}


def _dash(monkeypatch, ex_info, fund=97.43):
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: ex_info)
    monkeypatch.setattr(settings, "kss_first_wave_usd", 15.0)
    return PyramidSession(
        symbol="DASH", entry_price=34.47, distance_pct=2.0, max_waves=4,
        isolated_fund=fund, tp_pct=3.0, timeout_x_min=43200.0, gap_y_min=0.0,
    )


def test_coarse_step_does_not_produce_dust(monkeypatch):
    """The reported failure: wave 0 must not be the ~$0.03 dust order."""
    s = _dash(monkeypatch, _DASH_BAD)
    w0 = s.generate_wave(0)
    notional = w0.quantity * w0.target_price
    # The bug produced 0.001 units = ~$0.034. A real order is at least one whole lot.
    assert w0.quantity >= _DASH_BAD["stepSize"], "wave 0 collapsed below one lot (dust)"
    assert notional > 1.0, f"wave 0 notional ${notional:.4f} is a dust order"


def test_no_wave_is_dust(monkeypatch):
    """All three waves that DASH observed as [0.001, 0.001, 0.001] must each be a real lot."""
    s = _dash(monkeypatch, _DASH_BAD)
    for n in range(3):
        q = s.generate_wave(n).quantity
        assert q >= _DASH_BAD["stepSize"], f"wave {n} qty {q} is dust (< one lot)"


def test_fine_step_sizing_unchanged(monkeypatch):
    """With the correct DASH filter the fix must be a no-op: wave 0 ≈ $15 as before."""
    s = _dash(monkeypatch, _DASH_OK)
    w0 = s.generate_wave(0)
    assert w0.quantity == pytest.approx(15.0 / 34.47, rel=0.01)   # ≈ 0.435
    assert w0.quantity * w0.target_price == pytest.approx(15.0, rel=0.02)


def test_start_refuses_when_one_lot_exceeds_fund(monkeypatch):
    """Safe fallback: if the smallest tradeable lot costs more than the isolated fund, the
    session must NOT open (returns None) rather than open a dust or over-budget position."""
    # 1 lot = 1.0 DASH ≈ $34.47; a $10 fund can't afford it.
    s = _dash(monkeypatch, _DASH_BAD, fund=10.0)
    assert s.start() is None
