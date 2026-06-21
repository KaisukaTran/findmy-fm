"""Phase A: relative-strength-vs-BTC entry gate (app.scanner). Pure helpers, no network.

Key property: a coin UP while BTC is DOWN must PASS (it outperforms) — a BTC downtrend alone must
never block a strong alt. A coin materially weaker than BTC is blocked.
"""

from __future__ import annotations

import pytest

from app import scanner
from app.config import settings


def _candles(closes):
    return [{"close": c} for c in closes]


def test_nbar_return():
    assert scanner._nbar_return(_candles([100, 101, 110]), 2) == pytest.approx(10.0)  # 110 vs 100
    assert scanner._nbar_return(_candles([100]), 2) is None             # not enough data
    assert scanner._nbar_return([], 7) is None


def test_btc_ref_return_from_map():
    cmap = {"BTC": (_candles([100, 100, 105]), True)}
    assert scanner._btc_ref_return(cmap, 2) == pytest.approx(5.0)
    assert scanner._btc_ref_return({}, 2) is None                       # no BTC → None


def test_rel_strength_off_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "rel_strength_enabled", False)
    assert scanner._rel_strength_veto(_candles([100, 80]), btc_ret=5.0) is None


def test_rel_strength_blocks_coin_weaker_than_btc(monkeypatch):
    monkeypatch.setattr(settings, "rel_strength_enabled", True)
    monkeypatch.setattr(settings, "rel_strength_lookback_bars", 1)
    monkeypatch.setattr(settings, "rel_strength_margin_pct", 2.0)
    # coin −5% while BTC +1% → far weaker → blocked
    assert scanner._rel_strength_veto(_candles([100, 95]), btc_ret=1.0) is not None


def test_rel_strength_passes_strong_alt_even_when_btc_down(monkeypatch):
    monkeypatch.setattr(settings, "rel_strength_enabled", True)
    monkeypatch.setattr(settings, "rel_strength_lookback_bars", 1)
    monkeypatch.setattr(settings, "rel_strength_margin_pct", 2.0)
    # coin +3% while BTC −4% → outperforming → passes (BTC-down alone must not block)
    assert scanner._rel_strength_veto(_candles([100, 103]), btc_ret=-4.0) is None
    # coin lagging within the margin (coin −1%, BTC 0%, margin 2%) → passes
    assert scanner._rel_strength_veto(_candles([100, 99]), btc_ret=0.0) is None


def test_rel_strength_none_when_btc_missing(monkeypatch):
    monkeypatch.setattr(settings, "rel_strength_enabled", True)
    assert scanner._rel_strength_veto(_candles([100, 50]), btc_ret=None) is None


def test_rel_strength_knobs_runtime_tunable():
    from app.runtime import KSS_SETTING_FIELDS
    for k in ("rel_strength_enabled", "rel_strength_lookback_bars", "rel_strength_margin_pct"):
        assert k in KSS_SETTING_FIELDS
