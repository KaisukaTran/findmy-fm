"""Phase 3 of the loss-investigation project (docs/loss-cases.md): a SOFT overextension
rank-penalty in the scanner. Losing sessions tended to open when the coin had already run up
hard (higher N-bar return) — this de-prioritizes (never blocks) hot entries at open time by
subtracting a penalty from the ranking key's leading ``consensus`` term. Default OFF →
byte-identical behaviour to today (app.scanner._open_rank_key, docs/kss-strategy.md)."""

from __future__ import annotations

import pytest

from app import models, scanner
from app.config import settings

# Reuse test_scanner's fake-provider fixture (pytest binds fixtures by their local module-level
# name, so it must be imported unaliased as `scan_env` — each use as a test parameter below is a
# deliberate shadow, not an accidental redefinition, hence the per-line noqa: F811).
from tests.app.test_scanner import scan_env  # noqa: F401


def _candles(closes):
    return [{"close": c} for c in closes]


# ---------------------------------------------------------------------------
# _overextension_score: positive-only recent "heat"
# ---------------------------------------------------------------------------


def test_overextension_score_positive_runup(monkeypatch):
    monkeypatch.setattr(settings, "overextension_lookback_bars", 20)
    # 21 bars: 100 -> 113 over the last 20 bars = +13%
    closes = [100.0] + [101.0] * 19 + [113.0]
    assert scanner._overextension_score(_candles(closes)) == pytest.approx(13.0)


def test_overextension_score_flat_is_zero(monkeypatch):
    monkeypatch.setattr(settings, "overextension_lookback_bars", 20)
    closes = [100.0] * 21
    assert scanner._overextension_score(_candles(closes)) == pytest.approx(0.0)


def test_overextension_score_falling_is_zero_not_negative(monkeypatch):
    monkeypatch.setattr(settings, "overextension_lookback_bars", 20)
    closes = [100.0] * 20 + [90.0]  # -10% over the lookback
    assert scanner._overextension_score(_candles(closes)) == pytest.approx(0.0)


def test_overextension_score_too_few_candles_is_zero(monkeypatch):
    monkeypatch.setattr(settings, "overextension_lookback_bars", 20)
    assert scanner._overextension_score(_candles([100.0, 105.0])) == pytest.approx(0.0)
    assert scanner._overextension_score([]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _open_rank_key: disabled (default) → byte-identical to the pre-penalty tuple
# ---------------------------------------------------------------------------


def test_open_rank_key_disabled_ignores_overext_score():
    assert settings.overextension_penalty_enabled is False  # model default, not just this test
    hot = {"consensus": 60.0, "overext_score": 20.0, "worst_mae": -5.0,
           "win_rate_lb": 80.0, "expectancy": 3.0}
    cool = {"consensus": 60.0, "overext_score": 0.0, "worst_mae": -5.0,
            "win_rate_lb": 80.0, "expectancy": 3.0}
    expected = (60.0, -5.0, 80.0, 3.0)
    assert scanner._open_rank_key(hot) == expected
    assert scanner._open_rank_key(cool) == expected


# ---------------------------------------------------------------------------
# _open_rank_key: enabled → cooler coin outranks a hot one at equal consensus
# ---------------------------------------------------------------------------


def test_open_rank_key_enabled_prefers_cooler_coin(monkeypatch):
    monkeypatch.setattr(settings, "overextension_penalty_enabled", True)
    monkeypatch.setattr(settings, "overextension_penalty_weight", 0.5)
    hot = {"consensus": 60.0, "overext_score": 20.0, "worst_mae": 0.0,
           "win_rate_lb": 0.0, "expectancy": 0.0, "symbol": "HOT"}
    cool = {"consensus": 60.0, "overext_score": 0.0, "worst_mae": 0.0,
            "win_rate_lb": 0.0, "expectancy": 0.0, "symbol": "COOL"}
    ranked = sorted([hot, cool], key=scanner._open_rank_key, reverse=True)
    assert ranked[0]["symbol"] == "COOL"
    # effective consensus for hot is 60 - 0.5*20 = 50 < cool's 60
    assert scanner._open_rank_key(hot)[0] == pytest.approx(50.0)
    assert scanner._open_rank_key(cool)[0] == pytest.approx(60.0)


def test_overextension_penalty_never_blocks_only_reorders(monkeypatch):
    monkeypatch.setattr(settings, "overextension_penalty_enabled", True)
    monkeypatch.setattr(settings, "overextension_penalty_weight", 0.5)
    cands = [
        {"consensus": 60.0, "overext_score": 20.0, "worst_mae": 0.0,
         "win_rate_lb": 0.0, "expectancy": 0.0, "symbol": "HOT"},
        {"consensus": 60.0, "overext_score": 0.0, "worst_mae": 0.0,
         "win_rate_lb": 0.0, "expectancy": 0.0, "symbol": "COOL"},
        {"consensus": 50.0, "overext_score": 5.0, "worst_mae": -1.0,
         "win_rate_lb": 10.0, "expectancy": 1.0, "symbol": "OTHER"},
    ]
    ranked = sorted(cands, key=scanner._open_rank_key, reverse=True)
    assert len(ranked) == len(cands)
    assert {c["symbol"] for c in ranked} == {"HOT", "COOL", "OTHER"}


# ---------------------------------------------------------------------------
# Knob wiring: config Field + KssSettingsBody + runtime coercer dict
# ---------------------------------------------------------------------------


def test_overextension_knobs_default_off():
    assert settings.overextension_penalty_enabled is False
    assert settings.overextension_penalty_weight == pytest.approx(0.5)
    assert settings.overextension_lookback_bars == 20


def test_kss_settings_body_accepts_overextension_knobs():
    from app.routes import KssSettingsBody

    dumped = KssSettingsBody(
        overextension_penalty_enabled=True,
        overextension_penalty_weight=1.0,
        overextension_lookback_bars=30,
    ).model_dump(exclude_none=True)
    assert dumped["overextension_penalty_enabled"] is True
    assert dumped["overextension_penalty_weight"] == 1.0
    assert dumped["overextension_lookback_bars"] == 30


def test_runtime_coercer_maps_overextension_knobs():
    from app.runtime import KSS_SETTING_FIELDS

    assert KSS_SETTING_FIELDS["overextension_penalty_enabled"] is scanner.runtime._to_bool
    assert KSS_SETTING_FIELDS["overextension_penalty_weight"] is float
    assert KSS_SETTING_FIELDS["overextension_lookback_bars"] is int


def test_set_kss_settings_persists_and_restores_overextension(db):
    from app import runtime

    runtime.set_kss_settings(db, {
        "overextension_penalty_enabled": True,
        "overextension_penalty_weight": 0.75,
        "overextension_lookback_bars": 15,
    })
    assert settings.overextension_penalty_enabled is True
    assert settings.overextension_penalty_weight == 0.75
    assert settings.overextension_lookback_bars == 15
    # simulate restart
    settings.overextension_penalty_enabled = False
    settings.overextension_penalty_weight = 0.5
    settings.overextension_lookback_bars = 20
    runtime.sync_from_db(db)
    assert settings.overextension_penalty_enabled is True
    assert settings.overextension_penalty_weight == 0.75
    assert settings.overextension_lookback_bars == 15


# ---------------------------------------------------------------------------
# Runtime visibility: the compact tag on cand.reason (Kai requires this)
# ---------------------------------------------------------------------------


def test_overextension_tag_appears_on_reason_when_enabled(db, scan_env, monkeypatch):  # noqa: F811
    # scan_env's BTC fixture is a steady +1%/day uptrend → its 20-bar return is
    # well above 0, so the penalty (and its visibility tag) actually engages.
    monkeypatch.setattr(settings, "overextension_penalty_enabled", True)
    monkeypatch.setattr(settings, "overextension_penalty_weight", 0.5)
    monkeypatch.setattr(settings, "overextension_lookback_bars", 20)
    scanner.run_scan(db, mode="semi")
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert "overext" in (cand.reason or "")


def test_overextension_tag_absent_when_disabled(db, scan_env):  # noqa: F811
    scanner.run_scan(db, mode="semi")
    cand = db.query(models.Candidate).filter_by(symbol="BTC").one()
    assert "overext" not in (cand.reason or "")
