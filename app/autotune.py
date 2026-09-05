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

A third rule (relaxing `min_net_edge` against this same global ceiling) briefly lived here and
was REMOVED (2026-09-01, audit finding): unlike `min_expectancy_pct`, `min_net_edge` is
evaluated per candidate against that SYMBOL's own AUTOTUNED take-profit (`_effective_params`
in app.scanner), not the global `scan_tp_pct` — so a comparison against the global ceiling is
blind to what the gate is actually checked against, exactly the blindness
`app.scanner._tp_cannot_clear_gate` documents one level down. The removed rule could LOOSEN a
real risk gate for no reason (a low global tp with autotune_levels on rewrote min_net_edge
0.5 -> 0.32) while nothing it claimed to "unblock" was actually blocked.

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

# Stage 3 (learning from outcomes). Slow on purpose: a strategy that re-tunes itself on every
# result chases noise, and a target that swings around is worse than one that is slightly wrong.
LEARN_WINDOW = 60          # how many recent closed sessions count as evidence
LEARN_MIN_SESSIONS = 10    # below this it is a small sample, not a pattern
LEARN_MIN_NEW = 3          # below this many NEW closed sessions since the last move, it is
                           # the same evidence re-read, not a new result (2026-09 audit: one
                           # unchanged LEARN_WINDOW fired the rule 7 times off 13 sessions)
LEARN_STEP = 0.05          # one nudge per run, never more
TP_MULT_MIN = 0.3          # a target under ~a third of daily range is mostly fees
TP_MULT_MAX = 2.0          # beyond ~two days of range the deadline expires first
TIMEOUT_RATE_HIGH = 0.4    # this many timeouts means the target is out of reach
HIT_RATE_HIGH = 0.7        # winning this often AND fast means we are selling too early
FAST_HIT_SHARE = 0.25      # "fast" = using under a quarter of the deadline
_LEARN_WATERMARK_KEY = "autotune:learn:watermark"  # highest session id already consumed

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


def _learn_watermark(db: Session) -> int | None:
    """Highest closed-session id already used by a past adjustment, or None (never adjusted)."""
    raw = runtime.get(db, _LEARN_WATERMARK_KEY)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _closed_outcomes(db: Session, limit: int) -> list[dict]:
    """Recent finished sessions, classified by their REAL exit — not their status.

    Status alone cannot tell a real take-profit from anything else: a completed session
    lands on SESSION_COMPLETED (the transient SESSION_TP_TRIGGERED does not survive the
    close), and SESSION_STOPPED covers a hard stop-loss, a trailing exit, a manual stop AND
    a zero-fill zombie that never placed an order — none of which are a timeout. The only
    trustworthy signal is the newest SELL fill whose ``source_ref`` traces back to this
    session's own pyramid ladder (``pyramid:{id}:<reason>``); the suffix after the last ':'
    IS the exit reason.

    Returns dicts with ``id``, ``label`` (one of "hit", "timeout", "neither", "excluded")
    and ``share`` (fraction of the deadline used, from the exit fill's own timestamp — or
    None when there is no resolvable exit time). A session with no fill at all — it never
    traded — is labelled "excluded" and carries no evidence either way.
    """
    from app import models

    rows = (
        db.query(models.KssSession)
        .filter(models.KssSession.status.in_(
            (models.SESSION_STOPPED, models.SESSION_COMPLETED, models.SESSION_TP_TRIGGERED)))
        .order_by(models.KssSession.id.desc())
        .limit(limit)
        .all()
    )
    out: list[dict] = []
    for row in rows:
        fills = (
            db.query(models.Fill)
            .filter(models.Fill.source_ref.like(f"pyramid:{row.id}:%"))
            .order_by(models.Fill.executed_at.desc(), models.Fill.id.desc())
            .all()
        )
        if not fills:
            out.append({"id": row.id, "label": "excluded", "share": None})
            continue

        exit_fill = next((f for f in fills if f.side == "SELL"), None)
        suffix = (exit_fill.source_ref.rsplit(":", 1)[-1]
                  if exit_fill and exit_fill.source_ref else None)
        if suffix == "tp":
            label = "hit"
        elif suffix == "deadline":
            label = "timeout"
        else:
            # sl / trail_sl / manual_tp / an orphan sweep that left no own exit fill — real
            # evidence that the session traded, but neither a hit nor a timeout on THIS target.
            label = "neither"

        share = None
        if exit_fill is not None:
            started = row.started_at or row.created_at
            if started is not None:
                deadline_h = max(row.deadline_days, 0) * 24.0 or 1.0
                hours = max((exit_fill.executed_at - started).total_seconds() / 3600.0, 0.0)
                share = hours / deadline_h

        out.append({"id": row.id, "label": label, "share": share})
    return out


def learn_from_outcomes(db: Session) -> dict | None:
    """Nudge the take-profit multiple toward what the market has actually been paying.

    Stage 2 derives the target from volatility, which is a prediction. This checks it against
    closed sessions: a run of timeouts says the target sits too far out, while targets hit
    almost immediately say the opposite — the position was sold back into a move that had
    barely started.

    One small step per run, a minimum sample before it moves at all, a requirement for NEW
    evidence since the last move (a `runtime_config` watermark on session id — re-reading the
    same window is not a second result), hard clamps, and the evidence written to the audit
    log. It touches only how ambitious the target is; the risk gates are never learned.
    """
    if not (settings.autotune_enabled and settings.autotune_levels_enabled
            and settings.autotune_learn_enabled):
        return None

    outcomes = _closed_outcomes(db, LEARN_WINDOW)
    usable = [o for o in outcomes if o["label"] != "excluded"]
    if len(usable) < LEARN_MIN_SESSIONS:
        return None  # not evidence yet, just noise

    watermark = _learn_watermark(db)
    # NEW evidence is counted over the USABLE sessions only. A zero-fill zombie carries no
    # information about a take-profit target, so three of them must not unlock a step off
    # otherwise unchanged evidence — that is the re-reading defect in a smaller costume. The
    # watermark itself is still advanced across the WHOLE window below, so a zombie is
    # consumed once and can never be presented as new a second time.
    new_ids = [o["id"] for o in usable if watermark is None or o["id"] > watermark]
    if len(new_ids) < LEARN_MIN_NEW:
        return None  # same evidence as the last run — nothing new to learn from

    hit_count = sum(1 for o in usable if o["label"] == "hit")
    timeout_count = sum(1 for o in usable if o["label"] == "timeout")
    neither_count = len(usable) - hit_count - timeout_count
    excluded_count = len(outcomes) - len(usable)
    hit_shares = [o["share"] for o in usable if o["label"] == "hit" and o["share"] is not None]

    hit_rate = hit_count / len(usable)
    timeout_rate = timeout_count / len(usable)
    current = settings.autotune_tp_atr_mult

    if timeout_rate >= TIMEOUT_RATE_HIGH:
        target = max(current - LEARN_STEP, TP_MULT_MIN)
        reason = (f"{timeout_count}/{len(usable)} sessions timed out without reaching the "
                  f"target — it is too far out for how these coins actually move")
    elif hit_rate >= HIT_RATE_HIGH and hit_shares and (sum(hit_shares) / len(hit_shares)) <= FAST_HIT_SHARE:
        target = min(current + LEARN_STEP, TP_MULT_MAX)
        reason = (f"{hit_count}/{len(usable)} sessions hit the target using only "
                  f"{sum(hit_shares) / len(hit_shares) * 100:.0f}% of their deadline — selling "
                  "back into a move that had barely started")
    else:
        return None

    if abs(target - current) < 1e-9:
        return None  # already at the clamp

    settings.autotune_tp_atr_mult = round(target, 4)
    runtime.set(db, "kss:autotune_tp_atr_mult", settings.autotune_tp_atr_mult)
    runtime.set(db, _LEARN_WATERMARK_KEY, max(o["id"] for o in outcomes))
    logger.warning("autotune: tp_atr_mult %.3f -> %.3f (%s)", current, target, reason)
    audit.log(db, "autotune", "autotune_learn", entity="autotune_tp_atr_mult",
              before=round(current, 4), after=settings.autotune_tp_atr_mult,
              sessions=len(usable), hit_rate=round(hit_rate, 3),
              timeout_rate=round(timeout_rate, 3), hits=hit_count, timeouts=timeout_count,
              neither=neither_count, excluded=excluded_count,
              new_since_watermark=len(new_ids), reason=reason)
    db.commit()
    return {"setting": "autotune_tp_atr_mult", "before": current,
            "after": settings.autotune_tp_atr_mult, "reason": reason}


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
