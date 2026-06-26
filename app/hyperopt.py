"""
Per-pair KSS parameter search (Phase C).

Grid-searches the cartesian product of DISTANCE_GRID × TP_GRID × WAVES_GRID,
capped at ``settings.hyperopt_trials`` evaluations, and picks the combo with
the best out-of-sample score using the loss-minimising objective:

    score = win_rate - 1.5 * loss_rate

Only combos that clear the round-trip cost gate (``costengine.covers_costs``)
and produce at least 5 complete trials are eligible.  Results are persisted
in the ``pair_params`` table and can be queried with ``best_params``.
"""

from __future__ import annotations

from itertools import product

from sqlalchemy.orm import Session

import app.audit as audit
import app.costengine as costengine
from app.backtest import estimate_win_rate
from app.clock import utcnow
from app.config import settings
from app.data.providers import Candle, data_provider
from app.models import PairParams

# ---------------------------------------------------------------------------
# Grids
# ---------------------------------------------------------------------------

DISTANCE_GRID: list[float] = [1.5, 2.0, 2.5, 3.0]
TP_GRID: list[float] = [2.0, 3.0, 4.0, 5.0]
WAVES_GRID: list[int] = [6, 8, 10, 12]

_MIN_TRIALS = 5


# ---------------------------------------------------------------------------
# Core search
# ---------------------------------------------------------------------------


def optimize(candles: list[Candle]) -> dict | None:
    """Search the parameter grid and return the best combo, or None.

    Iterates up to ``settings.hyperopt_trials`` combinations of
    (distance_pct, tp_pct, max_waves) in a deterministic order.  Each combo
    must pass the cost gate and yield at least ``_MIN_TRIALS`` complete
    walk-forward trials to be eligible.

    Returns a dict with keys:
        distance_pct, tp_pct, max_waves, score, trials, win_rate, loss_rate
    or None if no eligible combo exists (too little data or all fail cost gate).
    """
    best: dict | None = None
    best_score = float("-inf")
    evaluated = 0
    cap = max(settings.hyperopt_trials, 1)

    for dist, tp, waves in product(DISTANCE_GRID, TP_GRID, WAVES_GRID):
        if evaluated >= cap:
            break

        if not costengine.covers_costs(tp):
            continue

        result = estimate_win_rate(
            candles,
            dist,
            waves,
            tp,
            settings.deadline_days,
            split=settings.walk_forward_split,
        )
        evaluated += 1

        if result["trials"] < _MIN_TRIALS:
            continue

        score = result["win_rate"] - 1.5 * result["loss_rate"]
        if score > best_score:
            best_score = score
            best = {
                "distance_pct": dist,
                "tp_pct": tp,
                "max_waves": waves,
                "score": round(score, 4),
                "trials": result["trials"],
                "win_rate": result["win_rate"],
                "loss_rate": result["loss_rate"],
            }

    return best


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist(db: Session, symbol: str, best: dict) -> PairParams:
    """Upsert the PairParams row for *symbol* from *best* and return it.

    Does not commit; caller is responsible for the transaction boundary when
    chaining with audit.log (see run_for).
    """
    row = db.get(PairParams, symbol)
    if row is None:
        row = PairParams(symbol=symbol)
        db.add(row)

    row.distance_pct = best["distance_pct"]
    row.tp_pct = best["tp_pct"]
    row.max_waves = best["max_waves"]
    row.score = best["score"]
    row.trials = best["trials"]
    row.updated_at = utcnow()

    db.flush()
    return row


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_for(
    db: Session,
    symbol: str,
    candles: list[Candle] | None = None,
) -> PairParams | None:
    """Run hyperopt for *symbol*, persist the winner, and audit the result.

    If *candles* is None, fetches history via the configured data provider.
    Returns the saved PairParams row on success, None when no eligible combo
    was found (too little data, all combos filtered by cost gate, etc.).
    """
    if candles is None:
        candles = data_provider().get_ohlcv(
            symbol,
            settings.backtest_timeframe,
            settings.backtest_lookback_days,
        )

    best = optimize(candles)

    if best is not None:
        row = persist(db, symbol, best)
        audit.log(db, "hyperopt", "tuned", entity=symbol, **best)
        db.commit()
        return row

    audit.log(db, "hyperopt", "no_fit", entity=symbol)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# Read helper
# ---------------------------------------------------------------------------


def best_params(db: Session, symbol: str) -> PairParams | None:
    """Return the stored PairParams for *symbol*, or None if not yet tuned."""
    return db.get(PairParams, symbol)
