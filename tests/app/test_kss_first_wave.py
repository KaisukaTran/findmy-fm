"""
Verification spec for `kss_first_wave_usd` (UX plan #2) — written by Opus; kss-strategy
implements until green.

Contract:
- When settings.kss_first_wave_usd > 0, a session's FIRST wave (wave 0) has a notional
  ≈ that USD value (within one step-size rounding), so the operator controls deployment.
- When it is 0 (default), sizing is the LEGACY pip_multiplier × minQty — i.e. the frozen
  invariants in test_kss_invariants.py must stay green (this file asserts that too).
- The (n+1)× pyramid shape is preserved: wave 1 ≈ 2× wave 0's qty.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kss.pyramid import PyramidSession

_EX = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


def _session(monkeypatch, fund: float = 100_000.0) -> PyramidSession:
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {"BTC": 50000.0})
    return PyramidSession(
        symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=5,
        isolated_fund=fund, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )


def test_first_wave_usd_sets_wave_zero_notional(monkeypatch):
    monkeypatch.setattr(settings, "kss_first_wave_usd", 100.0)
    s = _session(monkeypatch)
    w0 = s.generate_wave(0)
    notional = w0.quantity * w0.target_price
    assert notional == pytest.approx(100.0, rel=0.02)  # wave-0 ≈ $100


def test_first_wave_usd_preserves_pyramid_shape(monkeypatch):
    monkeypatch.setattr(settings, "kss_first_wave_usd", 100.0)
    s = _session(monkeypatch)
    q0 = s.generate_wave(0).quantity
    q1 = s.generate_wave(1).quantity
    assert q1 == pytest.approx(2 * q0, rel=0.02)  # (1+1)× vs (0+1)×


def test_zero_keeps_legacy_pip_sizing(monkeypatch):
    monkeypatch.setattr(settings, "kss_first_wave_usd", 0.0)
    s = _session(monkeypatch, fund=1000.0)
    assert s.pip_size == pytest.approx(0.00002)               # pip_multiplier(2) × minQty
    assert s.generate_wave(0).quantity == pytest.approx(0.00002)
