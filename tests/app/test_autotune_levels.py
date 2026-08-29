"""Self-adjusting levels — stage 2: fit the entry/exit distances to REALISED volatility.

One distance and one take-profit for every coin cannot be right: measured on the soak's own
universe, daily ATR ran from 3.9% (BTC) to 8.6% (XRP). Against that, the fixed 3.24% effective
TP is ~0.45 ATR on a typical coin — selling back most of a normal day's range — while the 2%
DCA step is ~0.28 ATR, close enough that ordinary noise walks the ladder down.

So each symbol gets levels derived from its own ATR, clamped so the result can never breach the
fee floor or run away to absurd numbers, and only ever applied to NEW sessions.
"""

from __future__ import annotations

import pytest

from app import autotune, costengine
from app.config import settings


def _candles(atr_pct: float, n: int = 30, close: float = 100.0) -> list[dict]:
    """Bars whose true range is a fixed % of close, so ATR% is exactly atr_pct."""
    half = close * atr_pct / 200.0
    return [
        {"ts": 1_700_000_000_000 + i * 86_400_000, "open": close, "high": close + half,
         "low": close - half, "close": close, "volume": 1000.0}
        for i in range(n)
    ]


def test_a_volatile_coin_gets_wider_levels_than_a_calm_one(db, monkeypatch):
    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)

    autotune.fit_levels(db, {"CALM": _candles(4.0), "WILD": _candles(9.0)})

    calm = autotune.levels_for(db, "CALM")
    wild = autotune.levels_for(db, "WILD")
    assert wild["tp_pct"] > calm["tp_pct"]
    assert wild["distance_pct"] > calm["distance_pct"]


def test_the_take_profit_tracks_atr_by_the_configured_multiple(db, monkeypatch):
    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)
    monkeypatch.setattr(settings, "autotune_tp_atr_mult", 0.8)

    autotune.fit_levels(db, {"X": _candles(7.0)})

    assert autotune.levels_for(db, "X")["tp_pct"] == pytest.approx(5.6, abs=0.3)


def test_a_take_profit_can_never_land_under_the_fee_floor(db, monkeypatch):
    """Even a dead-calm coin must not get a TP that loses money after fees."""
    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)
    monkeypatch.setattr(settings, "autotune_tp_atr_mult", 0.8)

    autotune.fit_levels(db, {"FLAT": _candles(0.05)})

    assert autotune.levels_for(db, "FLAT")["tp_pct"] >= costengine.min_profit_pct()


def test_absurd_volatility_is_clamped(db, monkeypatch):
    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)

    autotune.fit_levels(db, {"MOON": _candles(400.0)})

    lv = autotune.levels_for(db, "MOON")
    assert lv["tp_pct"] <= autotune.TP_MAX_PCT
    assert lv["distance_pct"] <= autotune.DCA_MAX_PCT


def test_too_little_history_is_left_to_the_global_defaults(db, monkeypatch):
    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)

    autotune.fit_levels(db, {"NEW": _candles(6.0, n=3)})

    assert autotune.levels_for(db, "NEW") is None


def test_stage_2_off_leaves_the_global_levels_in_charge(db, monkeypatch):
    """Stage 2 changes what the strategy DOES, so it is opt-in — stage 1 staying on must not
    drag it in."""
    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", False)

    autotune.fit_levels(db, {"X": _candles(7.0)})

    assert autotune.levels_for(db, "X") is None


def test_autotune_off_disables_the_levels_too(db, monkeypatch):
    monkeypatch.setattr(settings, "autotune_enabled", False)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)

    autotune.fit_levels(db, {"X": _candles(7.0)})

    assert autotune.levels_for(db, "X") is None


def test_the_scanner_uses_the_fitted_levels_for_a_new_session(db, monkeypatch):
    from app import scanner

    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)
    monkeypatch.setattr(settings, "hyperopt_enabled", False)
    autotune.fit_levels(db, {"X": _candles(9.0)})

    distance, tp, waves = scanner._effective_params(db, "X")

    lv = autotune.levels_for(db, "X")
    assert (distance, tp) == (lv["distance_pct"], lv["tp_pct"])
    assert waves == settings.scan_max_waves  # the ladder length is not volatility-derived


def test_a_symbol_without_fitted_levels_keeps_the_global_defaults(db, monkeypatch):
    from app import scanner

    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)
    monkeypatch.setattr(settings, "hyperopt_enabled", False)

    assert scanner._effective_params(db, "UNSEEN") == (
        settings.scan_distance_pct, settings.scan_tp_pct, settings.scan_max_waves,
    )


def test_hyperopt_still_wins_when_it_is_on(db, monkeypatch):
    """Autotune is the fallback for the many symbols hyperopt never tunes — not a replacement."""
    from app import models, scanner

    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)
    monkeypatch.setattr(settings, "hyperopt_enabled", True)
    db.add(models.PairParams(symbol="X", distance_pct=1.5, tp_pct=2.5, max_waves=6, score=1.0))
    db.commit()
    autotune.fit_levels(db, {"X": _candles(9.0)})

    assert scanner._effective_params(db, "X") == (1.5, 2.5, 6)


def test_refitting_replaces_the_previous_levels(db, monkeypatch):
    monkeypatch.setattr(settings, "autotune_enabled", True)
    monkeypatch.setattr(settings, "autotune_levels_enabled", True)

    autotune.fit_levels(db, {"X": _candles(4.0)})
    calm = autotune.levels_for(db, "X")["tp_pct"]
    autotune.fit_levels(db, {"X": _candles(9.0)})

    assert autotune.levels_for(db, "X")["tp_pct"] > calm
