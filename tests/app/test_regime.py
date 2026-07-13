"""
Tests for the pure market-wide BTC regime gate (app/regime.py).

SHADOW-FIRST classifier: 50d/200d SMA regime with a hysteresis band around the slow SMA to
avoid whipsaw. This module only classifies + persists — it never blocks anything itself (the
scanner decides whether to enforce; see tests/app/test_scanner_regime_gate.py for that wiring).
"""

from __future__ import annotations

from app import regime


# --- classify_raw -------------------------------------------------------------


def test_classify_raw_downtrend_is_risk_off():
    closes = [100, 95, 90, 85, 80, 75]
    assert regime.classify_raw(closes, fast=3, slow=5) == "risk_off"


def test_classify_raw_uptrend_is_risk_on():
    closes = [75, 80, 85, 90, 95, 100]
    assert regime.classify_raw(closes, fast=3, slow=5) == "risk_on"


def test_classify_raw_mixed_signal_is_neutral():
    # last_close (200) > sma_slow (120), but sma_fast (100) < sma_slow (120) — mixed.
    closes = [150, 150, 150, 50, 50, 200]
    assert regime.classify_raw(closes, fast=3, slow=5) == "neutral"


def test_classify_raw_thin_data_is_unknown():
    closes = [100, 100]  # fewer than slow=5
    assert regime.classify_raw(closes, fast=3, slow=5) == "unknown"


def test_classify_raw_exact_boundary_is_unknown_below_slow():
    closes = [100, 100, 100, 100]  # exactly slow-1
    assert regime.classify_raw(closes, fast=3, slow=5) == "unknown"


# --- apply_hysteresis -----------------------------------------------------------


def test_hysteresis_holds_risk_on_when_dip_is_inside_the_band():
    # last_close (98) sits above the lower band (~97.6) — inside the 2% band. HOLD risk_on.
    closes = [100, 100, 100, 100, 98]
    state = regime.apply_hysteresis("risk_on", closes, fast=2, slow=5, hysteresis_pct=2.0)
    assert state == "risk_on"


def test_hysteresis_flips_to_risk_off_when_dip_breaks_the_band():
    # last_close (95) is below the lower band (~97.0) AND fast < slow. Flips.
    closes = [100, 100, 100, 100, 95]
    state = regime.apply_hysteresis("risk_on", closes, fast=2, slow=5, hysteresis_pct=2.0)
    assert state == "risk_off"


def test_hysteresis_holds_risk_off_when_recovery_is_inside_the_band():
    # last_close (102) sits below the upper band (~102.4) — inside the band. HOLD risk_off.
    closes = [100, 100, 100, 100, 102]
    state = regime.apply_hysteresis("risk_off", closes, fast=2, slow=5, hysteresis_pct=2.0)
    assert state == "risk_off"


def test_hysteresis_flips_to_risk_on_when_recovery_breaks_the_band():
    # last_close (105) is above the upper band (~103.0) AND fast > slow. Flips (symmetric).
    closes = [100, 100, 100, 100, 105]
    state = regime.apply_hysteresis("risk_off", closes, fast=2, slow=5, hysteresis_pct=2.0)
    assert state == "risk_on"


def test_hysteresis_unknown_prev_adopts_raw():
    closes = [75, 80, 85, 90, 95, 100]  # raw == risk_on
    state = regime.apply_hysteresis("unknown", closes, fast=3, slow=5, hysteresis_pct=2.0)
    assert state == regime.classify_raw(closes, fast=3, slow=5) == "risk_on"


def test_hysteresis_empty_prev_adopts_raw():
    closes = [100, 95, 90, 85, 80, 75]  # raw == risk_off
    state = regime.apply_hysteresis("", closes, fast=3, slow=5, hysteresis_pct=2.0)
    assert state == "risk_off"


def test_hysteresis_thin_data_is_unknown_regardless_of_prev():
    closes = [100, 100]
    assert regime.apply_hysteresis("risk_on", closes, fast=3, slow=5, hysteresis_pct=2.0) == "unknown"


# --- evaluate (DB persistence) ---------------------------------------------------


def test_evaluate_persists_state(db, monkeypatch):
    monkeypatch.setattr("app.config.settings.regime_sma_fast", 2)
    monkeypatch.setattr("app.config.settings.regime_sma_slow", 5)
    monkeypatch.setattr("app.config.settings.regime_hysteresis_pct", 2.0)

    closes = [100, 100, 100, 100, 95]  # first read: no prior state -> adopts raw (risk_off)
    result = regime.evaluate(db, closes)

    assert result["state"] == "risk_off"
    from app import runtime

    assert runtime.get(db, regime.KEY_REGIME_STATE) == "risk_off"


def test_evaluate_whipsaw_within_band_does_not_flip_across_two_calls(db, monkeypatch):
    monkeypatch.setattr("app.config.settings.regime_sma_fast", 2)
    monkeypatch.setattr("app.config.settings.regime_sma_slow", 5)
    monkeypatch.setattr("app.config.settings.regime_hysteresis_pct", 2.0)

    first = regime.evaluate(db, [100, 100, 100, 100, 95])
    assert first["state"] == "risk_off"

    # Raw would flip to risk_on (last_close 102 > sma_slow 100.4 AND fast > slow), but 102 sits
    # INSIDE the hysteresis band (~102.4) around the slow SMA -> the persisted state HOLDS.
    second = regime.evaluate(db, [100, 100, 100, 100, 102])
    assert second["state"] == "risk_off"

    from app import runtime

    assert runtime.get(db, regime.KEY_REGIME_STATE) == "risk_off"


def test_evaluate_never_raises_on_empty_closes(db, monkeypatch):
    monkeypatch.setattr("app.config.settings.regime_sma_fast", 2)
    monkeypatch.setattr("app.config.settings.regime_sma_slow", 5)

    result = regime.evaluate(db, [])
    assert result["state"] == "unknown"
