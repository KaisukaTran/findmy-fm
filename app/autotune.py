"""Self-correcting entry/exit levels.

The scanner's gates are arithmetic, and arithmetic can contradict itself. `min_expectancy_pct`
is compared against an expectancy that can never exceed `scan_tp_pct` minus the round-trip
cost, so a gate above that ceiling skips 100% of the universe forever — no error, no trades,
nothing to notice. That happened for 12 hours on paper and again on live.

**Stage 1 (here): keep the gates satisfiable.** Detect settings that contradict each other and
move them back into the reachable range, loudly and in the audit log. Two rules only, both
narrow:

  * `min_expectancy_pct` must sit under `expectancy_ceiling_pct(scan_tp_pct)`;
  * `scan_tp_pct` must clear the fee floor (`min_profit_pct`), or a "win" loses money.

What it deliberately does NOT do: it never tightens a gate, never touches one that is merely
strict, and never invents a value from a model's opinion. A contradiction has one arithmetic
answer; a preference does not, and preferences stay the operator's. Later stages (fitting the
levels to realised volatility, and learning from closed sessions) build on this one.

Off by `autotune_enabled=false`, in which case a broken setting is left broken — visibly.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app import audit, costengine, runtime
from app.config import settings

logger = logging.getLogger(__name__)

# Clamps for the volatility fit. A derived level is still a level that risks money, so it never
# leaves this range no matter what the ATR says — a data glitch or a coin that moved 400% in a
# day must not produce a ladder nobody would place by hand.
TP_MAX_PCT = 15.0
DCA_MIN_PCT = 0.5
DCA_MAX_PCT = 10.0
_ATR_BARS = 14
_MIN_BARS = 15  # ATR needs a previous close per bar, so 14 ranges want 15 candles
_LEVELS_KEY = "autotune:levels:"

# How far under the ceiling a corrected gate lands. Sitting exactly ON the ceiling would only
# admit a coin that won every single backtest trial, which is the deadlock all over again.
_GATE_HEADROOM = 0.8


def _apply(db: Session, key: str, value: float, reason: str) -> dict:
    """Change one knob, persist it (so a restart keeps the correction) and record why."""
    before = getattr(settings, key)
    setattr(settings, key, value)
    runtime.set(db, f"kss:{key}", value)
    logger.warning("autotune: %s %.4f -> %.4f (%s)", key, before, value, reason)
    audit.log(db, "autotune", "autotune", entity=key,
              before=round(before, 4), after=round(value, 4), reason=reason)
    return {"setting": key, "before": before, "after": value, "reason": reason}


def enforce_consistency(db: Session) -> list[dict]:
    """Bring contradictory entry/exit settings back into a range that can actually trade.

    Returns the changes made (empty when nothing was contradictory). Safe to call every
    cycle: it is idempotent, and a settled configuration costs two comparisons.
    """
    if not settings.autotune_enabled:
        return []

    changes: list[dict] = []

    # A take-profit under the fee floor cannot clear its own round trip: the strategy would
    # book "wins" that lose money. Raise it first — the expectancy ceiling depends on it.
    floor = costengine.min_profit_pct()
    if settings.scan_tp_pct < floor:
        changes.append(_apply(
            db, "scan_tp_pct", round(floor, 4),
            f"take-profit {settings.scan_tp_pct:.2f}% is below the fee floor {floor:.2f}% — "
            "a win at that target would not cover its own round-trip fee",
        ))

    # The gate that skipped the whole universe in silence.
    ceiling = costengine.expectancy_ceiling_pct(settings.scan_tp_pct)
    if costengine.expectancy_gate_unsatisfiable(settings.min_expectancy_pct,
                                                settings.scan_tp_pct):
        target = round(max(ceiling * _GATE_HEADROOM, 0.01), 4)
        changes.append(_apply(
            db, "min_expectancy_pct", target,
            f"expectancy gate {settings.min_expectancy_pct:.2f}% is above the ceiling "
            f"{ceiling:.2f}% that a {settings.scan_tp_pct:.2f}% take-profit can reach — no "
            "candidate could ever pass it, so the scanner skipped every coin",
        ))

    if changes:
        db.commit()
    return changes


# --- stage 2: fit the levels to realised volatility -------------------------


def atr_pct(candles: list[dict]) -> float:
    """Average true range over the last bars, as a percent of close (0.0 = not enough data).

    Percent, not price, so one number compares BTC to a sub-cent coin — which is the whole
    point: the soak's universe ranged from 3.9%/day to 8.6%/day, and a single global distance
    cannot suit both.
    """
    if len(candles) < _MIN_BARS:
        return 0.0
    ranges = []
    for prev, bar in zip(candles[-_ATR_BARS - 1:-1], candles[-_ATR_BARS:], strict=False):
        close = float(bar["close"]) or 1.0
        tr = max(
            float(bar["high"]) - float(bar["low"]),
            abs(float(bar["high"]) - float(prev["close"])),
            abs(float(bar["low"]) - float(prev["close"])),
        )
        ranges.append(tr / close * 100.0)
    return sum(ranges) / len(ranges) if ranges else 0.0


def fit_levels(db: Session, candles_by_symbol: dict[str, list[dict]]) -> dict[str, dict]:
    """Derive per-symbol take-profit and DCA step from each symbol's own ATR.

    A take-profit far under a coin's daily range hands most of the move back; a DCA step far
    under it gets walked down by ordinary noise. Both are therefore multiples of ATR
    (`autotune_tp_atr_mult`, `autotune_dca_atr_mult`), clamped to a sane band and never below
    the fee floor. Stored per symbol and read by the scanner when it opens a NEW session —
    running sessions keep the levels they were opened with.
    """
    if not (settings.autotune_enabled and settings.autotune_levels_enabled):
        return {}

    floor = costengine.min_profit_pct()
    out: dict[str, dict] = {}
    for symbol, candles in candles_by_symbol.items():
        atr = atr_pct(candles or [])
        if atr <= 0:
            continue  # too little history — the global defaults still apply
        tp = min(max(atr * settings.autotune_tp_atr_mult, floor), TP_MAX_PCT)
        dca = min(max(atr * settings.autotune_dca_atr_mult, DCA_MIN_PCT), DCA_MAX_PCT)
        level = {"tp_pct": round(tp, 4), "distance_pct": round(dca, 4), "atr_pct": round(atr, 4)}
        runtime.set(db, f"{_LEVELS_KEY}{symbol}", json.dumps(level))
        out[symbol] = level
    if out:
        db.commit()
        logger.info("autotune: fitted levels for %d symbols (ATR-derived)", len(out))
    return out


def levels_for(db: Session, symbol: str) -> dict | None:
    """The fitted levels for *symbol*, or None when there are none (or the feature is off)."""
    if not (settings.autotune_enabled and settings.autotune_levels_enabled):
        return None
    raw = runtime.get(db, f"{_LEVELS_KEY}{symbol}")
    if not raw:
        return None
    try:
        level = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return level if {"tp_pct", "distance_pct"} <= level.keys() else None
