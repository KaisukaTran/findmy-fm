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

import logging

from sqlalchemy.orm import Session

from app import audit, costengine, runtime
from app.config import settings

logger = logging.getLogger(__name__)

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
