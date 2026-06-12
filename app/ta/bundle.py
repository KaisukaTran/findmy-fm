"""Assemble a compact, token-budgeted TA bundle for the Grok gate.

One flat dict of scalars per symbol (short keys, rounded floats) — the whole short-list is
batched into a single Grok call, so every byte here is multiplied by the candidate count.
Tier 1 (pure-Python) is always computed; Tier 2 (pandas-ta) and Tier 3 (external) overlay
their keys only when enabled, and each tier is fail-open so a scan never breaks.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.data.providers import Candle
from app.ta import indicators

log = logging.getLogger(__name__)


def _tier1(candles: list[Candle]) -> dict[str, Any]:
    cs = [c["close"] for c in candles]
    macd = indicators.macd(candles)
    bb = indicators.bollinger(candles)
    adx = indicators.adx(candles)
    sr = indicators.support_resistance(candles)
    vol = indicators.volume_trend(candles)
    return {
        "rsi": round(indicators.rsi(cs), 1),
        "macd_h": round(macd["hist_pct"], 2),
        "bb_pct": round(bb["pct_b"], 2),
        "bb_w": round(bb["bandwidth"], 2),
        "adx": round(adx["adx"], 1),
        "di": "up" if adx["plus_di"] >= adx["minus_di"] else "down",
        "atr_pct": round(indicators.atr_pct(candles), 2),
        "st": indicators.supertrend(candles),
        "htf": indicators.htf_trend(candles),
        "sr_res": round(sr["res_dist_pct"], 2),
        "sr_sup": round(sr["sup_dist_pct"], 2),
        "vtrend": "up" if vol["obv_up"] else "down",
        "vol_r": round(vol["vol_ratio"], 2),
    }


def build(
    candles: list[Candle],
    db: Session | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Return the merged TA bundle. Tier 1 always; Tier 2/3 overlay when enabled.

    `db`/`symbol` are only needed by Tier 3 (audit + per-scan cache); Tier 1 ignores them
    so callers and tests can pass candles alone.
    """
    out = _tier1(candles)

    if settings.ta_lib_enabled:
        try:
            from app.ta import lib

            out.update(lib.enrich(candles))
        except Exception as exc:  # noqa: BLE001 — fail-open to Tier 1
            log.warning("TA Tier 2 (lib) unavailable, using pure-Python: %s", type(exc).__name__)

    if settings.ta_external_enabled and symbol:
        try:
            from app.ta import external

            extra = external.fetch(db, symbol)
            if extra:
                out.update(extra)
        except Exception as exc:  # noqa: BLE001 — fail-open, never block a scan
            log.warning("TA Tier 3 (external) failed for %s: %s", symbol, type(exc).__name__)

    return out
