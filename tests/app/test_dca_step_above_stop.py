"""
A DCA rung below the hard stop-loss is a rung that can never fill.

WHAT HAPPENED (live, 2026-09-05 → 2026-09-07). `autotune_dca_atr_mult` was raised 0.5 → 1.5, so
the per-coin DCA step became 1.5x each coin's daily ATR and 41% of the universe pinned at the
`DCA_MAX_PCT = 10%` clamp. The hard stop-loss is 8% below average. **52% of the universe (114 of
218 coins) ended up with its first rung at or below the stop**, which the stop reaches first — so
the ladder could not fill a single rung. `wave_below_sl` went from 1 refusal in the project's
whole history to 26 in one day.

KSS is a pyramid-DCA strategy; a session that cannot average down is just "buy once, take profit
or stop out". The backtest measured exactly that and the loss-rate rose, which then failed the
entry gates (`min_win_rate` 60 on a Wilson lower bound, `max_loss_rate` 20). Sessions opened per
day went 7 → 3 → 0, and the operator found the app standing still with $200k of capital.

The knob is back at 0.5, but the knob was never the invariant. `DCA_MAX_PCT` is 10% while
`sl_pct` defaults to 8%, so even at the DEFAULT multiplier any coin with ATR >= 20%/day still
gets a dead ladder. Two numbers chosen independently, in different modules, that only make sense
relative to each other — the same shape as `kss_trail_lock_pct` vs `kss_trail_min_pct`, and as
`min_expectancy_pct` vs the take-profit ceiling that `costengine` already guards.

The rule these tests pin: a fitted DCA step must leave the first rung strictly ABOVE the hard
stop, whatever the multiplier says.
"""

from __future__ import annotations

import json

import pytest

from app import autotune, runtime
from app.config import settings


@pytest.fixture(autouse=True)
def _levels_on(monkeypatch):
    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)
    monkeypatch.setattr(settings, "sl_pct", 8.0)


def _fit(db, monkeypatch, atr: float, mult: float) -> dict:
    """Fit one symbol whose daily ATR% is exactly `atr`."""
    monkeypatch.setattr(settings, "autotune_dca_atr_mult", mult)
    monkeypatch.setattr(autotune, "atr_pct", lambda candles: atr)
    autotune.fit_levels(db, {"AAA": [{"high": 1, "low": 1, "close": 1}] * 30})
    return json.loads(runtime.get(db, "autotune:levels:AAA"))


class TestTheFirstRungStaysAboveTheStop:
    @pytest.mark.parametrize("atr,mult", [(11.9, 1.5), (20.0, 0.5), (43.9, 0.9), (72.9, 0.5)])
    def test_a_volatile_coin_never_gets_a_rung_under_the_stop(self, db, monkeypatch, atr, mult):
        # 11.9/1.5 is live 0G; 43.9 and 72.9 are live HFT and NFP, whose ATR alone breaks the
        # 10% clamp at the DEFAULT multiplier.
        level = _fit(db, monkeypatch, atr, mult)
        assert level["distance_pct"] < settings.sl_pct, (
            f"first rung at -{level['distance_pct']}% sits at/below the -{settings.sl_pct}% stop, "
            f"so the ladder can never fill")

    def test_the_step_keeps_real_room_not_just_a_hair(self, db, monkeypatch):
        # A rung a hair above the stop is re-anchored to the live market before it is queued
        # (`_anchor_dca_price`), so it needs margin, not just inequality.
        level = _fit(db, monkeypatch, 30.0, 1.5)
        assert level["distance_pct"] <= settings.sl_pct * 0.8

    def test_a_calm_coin_is_untouched(self, db, monkeypatch):
        # The clamp must only bind where the ladder would be dead — it is not a global narrowing.
        level = _fit(db, monkeypatch, 6.0, 0.5)          # 6.0 x 0.5 = 3.0%, well clear of the stop
        assert level["distance_pct"] == pytest.approx(3.0)

    def test_a_wider_stop_allows_a_wider_step(self, db, monkeypatch):
        # The ceiling is a RELATIONSHIP, not a new constant: raise the stop and the ladder may
        # widen with it.
        monkeypatch.setattr(settings, "sl_pct", 20.0)
        level = _fit(db, monkeypatch, 30.0, 0.5)
        assert level["distance_pct"] > 8.0

    def test_the_clamp_says_so_out_loud(self, db, monkeypatch):
        # Silence is how this cost three days of not trading. A bound clamp must leave evidence.
        from app import models
        _fit(db, monkeypatch, 40.0, 1.5)
        rows = db.query(models.AuditLog).filter_by(action="dca_step_clamped").all()
        assert rows, "the ladder was silently narrowed"
