"""
Market-wide BTC regime gate (docs: shadow-first risk-off classifier).

The bot had no market-wide risk-off device: per-coin vetoes (downtrend/falling-knife/
relative-strength) can't stop a CORRELATED selloff across the whole book — 17/18 of the
historical loss cases were stop-loss cascades in a broad market dump. This module classifies
BTC's daily trend (50d/200d SMA, with a hysteresis band around the 200d line to avoid
whipsaw) into risk_on/risk_off/neutral/unknown, and persists the state across scans.

SHIPPED SHADOW-FIRST: this module only classifies + persists. It NEVER blocks anything by
itself — the scanner decides whether to audit-only (shadow) or actually block new opens
(enforcing), and only for NEW SESSION OPENS. It never touches exits (SELLs) or DCA waves.

Pure + testable: no imports from app.scanner/app.kss (leaf module), reuses app.ta.indicators.sma.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app import runtime
from app.config import settings
from app.ta.indicators import sma

logger = logging.getLogger(__name__)

KEY_REGIME_STATE = "regime_state"

RISK_OFF = "risk_off"
RISK_ON = "risk_on"
NEUTRAL = "neutral"
UNKNOWN = "unknown"


def classify_raw(closes: list[float], fast: int, slow: int) -> str:
    """Classify the RAW (no-hysteresis) regime from a daily close series.

    Needs >= slow closes; returns 'unknown' otherwise (never blocks on thin data).
    'risk_off': last_close < sma_slow AND sma_fast < sma_slow (price and the fast average
    both below the 200d line). 'risk_on': the symmetric opposite (both above). Anything else
    (mixed signals) is 'neutral'.
    """
    if len(closes) < slow:
        return UNKNOWN
    sma_fast = sma(closes, fast)
    sma_slow = sma(closes, slow)
    last_close = closes[-1]
    if last_close < sma_slow and sma_fast < sma_slow:
        return RISK_OFF
    if last_close > sma_slow and sma_fast > sma_slow:
        return RISK_ON
    return NEUTRAL


def apply_hysteresis(
    prev_state: str, closes: list[float], fast: int, slow: int, hysteresis_pct: float
) -> str:
    """Apply a band around the 200d SMA to prevent whipsaw flip/flop around the line.

    To flip INTO risk_off from a non-risk_off state, price must close BELOW the band
    (sma_slow * (1 - hysteresis_pct/100)) AND the fast SMA must be below the slow SMA. To flip
    INTO risk_on, the symmetric ABOVE-band condition. Otherwise the previous state HOLDS — a
    close that dips just under the 200d line but stays inside the band does not flip a
    standing risk_on. An unknown/empty prev_state simply adopts the raw classification (no
    prior state to hold onto). Thin data (< slow closes) always returns 'unknown', regardless
    of prev_state, so the gate never blocks on insufficient history.
    """
    if len(closes) < slow:
        return UNKNOWN

    raw = classify_raw(closes, fast, slow)
    if not prev_state or prev_state == UNKNOWN:
        return raw

    sma_fast = sma(closes, fast)
    sma_slow = sma(closes, slow)
    last_close = closes[-1]
    lower_band = sma_slow * (1 - hysteresis_pct / 100)
    upper_band = sma_slow * (1 + hysteresis_pct / 100)

    if prev_state != RISK_OFF and last_close < lower_band and sma_fast < sma_slow:
        return RISK_OFF
    if prev_state != RISK_ON and last_close > upper_band and sma_fast > sma_slow:
        return RISK_ON
    if prev_state in (RISK_OFF, RISK_ON):
        return prev_state
    # prev_state == NEUTRAL and neither flip condition fired: adopt the raw read (neutral has
    # no "standing" bias to hold — it is not itself a directional call).
    return raw


def evaluate(db: Session, closes: list[float]) -> dict:
    """Compute + persist the effective regime state from a BTC daily close series.

    Reads the previously persisted state (runtime_config key KEY_REGIME_STATE), applies
    hysteresis against ``settings.regime_sma_fast/regime_sma_slow/regime_hysteresis_pct``,
    persists the new effective state, and returns a snapshot dict. Never raises: any error
    (bad data, DB hiccup) returns ``{"state": "unknown"}`` WITHOUT persisting, so a transient
    glitch never corrupts the standing state or crashes the scan.
    """
    try:
        fast = settings.regime_sma_fast
        slow = settings.regime_sma_slow
        hysteresis_pct = settings.regime_hysteresis_pct

        prev_state = runtime.get(db, KEY_REGIME_STATE) or UNKNOWN
        state = apply_hysteresis(prev_state, closes, fast, slow, hysteresis_pct)

        sma_fast = sma(closes, fast) if len(closes) >= slow else 0.0
        sma_slow = sma(closes, slow) if len(closes) >= slow else 0.0
        last_close = closes[-1] if closes else 0.0

        runtime.set(db, KEY_REGIME_STATE, state)

        return {
            "state": state,
            "last_close": last_close,
            "sma_fast": sma_fast,
            "sma_slow": sma_slow,
        }
    except Exception:
        logger.exception("regime.evaluate failed — returning unknown, not persisting")
        return {"state": UNKNOWN}
