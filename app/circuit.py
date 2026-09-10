"""
Capital-preservation circuit breaker for FINDMY-FM full-auto.

Trips the runtime freeze when drawdown, daily-loss, or consecutive-loss
thresholds are breached. Auto-rearms after a cooldown if all metrics clear.
Safe to call every scheduler cycle.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app import audit, portfolio, risk, runtime
from app.clock import utcnow
from app.config import settings
from app.models import Fill

# Reviewers that must be blocked when the breaker is frozen.
AUTO_REVIEWERS: frozenset[str] = frozenset({"auto-trader", "auto-approver", "scheduler", "opus"})

# Reason CODES. Every decision below keys off these; the text beside them is for humans
# only. `blocking` used to filter on the substring "consecutive_losses" in the displayed
# wording, so editing that wording silently turned the streak freeze into a permanent
# deadlock — it locked the account once. Change the text freely; never the code.
CODE_DRAWDOWN = "drawdown"
CODE_DAILY_LOSS = "daily_loss"
CODE_LOSS_STREAK = "loss_streak"

_TEXT_DRAWDOWN = "drawdown {n:.1f}% > limit {limit}%"
_TEXT_DAILY_LOSS = "daily_loss {n:.1f}% > limit {limit}%"
_TEXT_LOSS_STREAK = "consecutive_losses {n} >= limit {limit}"

# Only CURRENT-state reasons keep the freeze past the cooldown. A streak is historical:
# while frozen no new trades happen, so it can never clear on its own.
_CURRENT_STATE_CODES: frozenset[str] = frozenset({CODE_DRAWDOWN, CODE_DAILY_LOSS})

# SELL fills to scan for the grouped counter. Grouping collapses a whole session's exits
# into one event, so the legacy depth of 20 fills would often reach back only a handful of
# events.
_STREAK_FILL_SCAN = 200

# Runtime key holding the last audited shadow divergence, so a divergence that persists
# across scheduler cycles is logged once instead of every cycle.
KEY_SHADOW_MARK = "breaker_streak_shadow_mark"


def _recent_sell_fills(db: Session, limit: int) -> list[Fill]:
    return (
        db.query(Fill)
        .filter(Fill.side == "SELL")
        .order_by(Fill.executed_at.desc())
        .limit(limit)
        .all()
    )


def _consecutive_losses(db: Session) -> int:
    """LEGACY counter: leading SELL fills with realized_pnl < 0, most-recent first.

    Kept as the shadow control arm. It over-counts in two ways that `loss_clusters`
    fixes: a session exiting through several fills is counted once per fill, and one
    market drop that stops several sessions within seconds is counted once per session.
    """
    count = 0
    for f in _recent_sell_fills(db, 20):
        if f.realized_pnl < 0:
            count += 1
        else:
            break
    return count


def _exit_event_key(fill: Fill) -> str:
    """The unit an exit belongs to: its KSS session, else the fill itself.

    KSS stamps every exit with ``pyramid:<session_id>:<suffix>`` (tp / sl / trail_sl /
    manual_tp). A fill without that provenance — a manual sell — is its own event.
    """
    ref = fill.source_ref or ""
    if ref.startswith("pyramid:"):
        parts = ref.split(":")
        if len(parts) >= 2 and parts[1]:
            return f"session:{parts[1]}"
    return f"fill:{fill.id}"


def loss_clusters(db: Session) -> list[dict]:
    """Recent exits as clusters, newest first.

    Two collapses, in order:

    1. **By session** — every exit fill of one session sums into a single event whose
       P&L is the session's net outcome (a partial take-profit followed by a small stop
       is one WIN, not a win and a loss) and whose timestamp is its newest fill.
    2. **By time** — events landing within ``breaker_loss_cluster_sec`` of a cluster's
       newest member join it. One dip that stops five sessions is one signal.

    The window is measured from the cluster's newest member, NOT chained from the last
    one added: chaining would let a slow bleed of exits 299s apart fuse into a single
    cluster and hide a real losing run.
    """
    window = max(0.0, float(settings.breaker_loss_cluster_sec))

    events: dict[str, dict] = {}
    for f in _recent_sell_fills(db, _STREAK_FILL_SCAN):
        key = _exit_event_key(f)
        ev = events.get(key)
        if ev is None:
            events[key] = {"key": key, "pnl": f.realized_pnl, "at": f.executed_at,
                           "symbols": {f.symbol}, "fills": 1}
            continue
        ev["pnl"] += f.realized_pnl
        ev["fills"] += 1
        ev["symbols"].add(f.symbol)
        if f.executed_at > ev["at"]:
            ev["at"] = f.executed_at

    clusters: list[dict] = []
    for ev in sorted(events.values(), key=lambda e: e["at"], reverse=True):
        head = clusters[-1] if clusters else None
        if head is not None and (head["at"] - ev["at"]).total_seconds() <= window:
            head["pnl"] += ev["pnl"]
            head["events"] += 1
            head["symbols"].update(ev["symbols"])
        else:
            clusters.append({"at": ev["at"], "pnl": ev["pnl"], "events": 1,
                             "symbols": set(ev["symbols"])})
    return clusters


def _consecutive_loss_events(db: Session) -> int:
    """Leading loss CLUSTERS, most-recent first — the grouped replacement counter."""
    count = 0
    for c in loss_clusters(db):
        if c["pnl"] < 0:
            count += 1
        else:
            break
    return count


def metrics(db: Session) -> dict:
    """Return current circuit-breaker metrics."""
    perf = portfolio.performance_view(db)
    eq = max(portfolio.equity(db), 1e-9)
    dl = risk.daily_loss(db)
    return {
        # CURRENT drawdown, not the all-time worst. The breaker keeps the freeze while a
        # "current-state" reason is true, and a historical maximum is never false again — so
        # gating on it froze the account permanently after one dip, manual reset included.
        "drawdown_pct": perf.get("current_drawdown_pct", perf["max_drawdown_pct"]),
        "daily_loss_pct": dl / eq * 100,
        # Both counters are always reported. `consecutive_losses` is the legacy raw-fill
        # count; `consecutive_loss_events` groups by session and collapses a ~300s window.
        # Which one DECIDES is `breaker_streak_shadow` — see evaluate().
        "consecutive_losses": _consecutive_losses(db),
        "consecutive_loss_events": _consecutive_loss_events(db),
    }


def _audit_shadow_divergence(
    db: Session, *, raw: int, grouped: int, limit: int, shadow: bool
) -> None:
    """Record when the two streak rules would decide differently.

    This is the scoring mechanism for the shadow run (the `opus_shadow` precedent): only
    a disagreement AT THE THRESHOLD matters, and a disagreement that persists across
    scheduler cycles is logged once, not every cycle.
    """
    mark = "" if (raw >= limit) == (grouped >= limit) else f"{raw}:{grouped}:{limit}"
    if runtime.get(db, KEY_SHADOW_MARK, "") == mark:
        return  # nothing changed — never write on the quiet path, this runs every cycle
    runtime.set(db, KEY_SHADOW_MARK, mark)
    if not mark:
        return  # divergence cleared
    audit.log(db, "circuit", "shadow_divergence",
              raw=raw, grouped=grouped, limit=limit, shadow=shadow,
              deciding="legacy" if shadow else "grouped")
    db.commit()


def evaluate(db: Session) -> dict:
    """Evaluate breaker thresholds; trip freeze or auto-rearm as needed.

    Safe to call every scheduler cycle — idempotent when state is stable.
    """
    m = metrics(db)
    limit = settings.max_consecutive_losses

    # The grouped counter falls back to the legacy one when absent: tests (and any caller)
    # that patch metrics() with the three original keys must not KeyError here.
    raw_streak = m["consecutive_losses"]
    grouped_streak = m.get("consecutive_loss_events", raw_streak)
    shadow = settings.breaker_streak_shadow
    streak = raw_streak if shadow else grouped_streak

    coded: list[tuple[str, str]] = []
    if m["drawdown_pct"] > settings.max_drawdown_pct:
        coded.append((CODE_DRAWDOWN, _TEXT_DRAWDOWN.format(
            n=m["drawdown_pct"], limit=settings.max_drawdown_pct)))
    if m["daily_loss_pct"] > settings.daily_loss_hard_pct:
        coded.append((CODE_DAILY_LOSS, _TEXT_DAILY_LOSS.format(
            n=m["daily_loss_pct"], limit=settings.daily_loss_hard_pct)))
    if streak >= limit:
        coded.append((CODE_LOSS_STREAK, _TEXT_LOSS_STREAK.format(n=streak, limit=limit)))

    reasons: list[str] = [text for _, text in coded]

    _audit_shadow_divergence(db, raw=raw_streak, grouped=grouped_streak,
                             limit=limit, shadow=shadow)

    currently_frozen = runtime.is_frozen(db)

    # A consecutive-loss STREAK is historical: while frozen no new trades happen, so the
    # streak can never clear → it must NOT block auto-rearm, or a loss-streak freeze
    # deadlocks forever. The cooldown time-out is the streak's reset. Only CURRENT-state
    # reasons (drawdown, daily-loss) keep the breaker frozen past the cooldown.
    blocking = [text for code, text in coded if code in _CURRENT_STATE_CODES]

    if reasons and not currently_frozen:
        reason_str = "; ".join(reasons)
        runtime.freeze(db, reason_str)
        audit.log(db, "circuit", "freeze", detail={"reasons": reasons, **m})
        db.commit()
        try:
            from app import notify  # lazy — circuit must not import notify at module top
            notify.event("risk", f"🧊 Circuit breaker FROZEN: {reason_str}")
        except Exception:
            pass  # notify failure must never break evaluate

    elif currently_frozen and not blocking:
        # Attempt auto-rearm only after cooldown has elapsed (a stale loss-streak alone no
        # longer blocks it).
        frozen_at_raw = runtime.get(db, runtime.KEY_FROZEN_AT)
        if frozen_at_raw:
            try:
                frozen_at = datetime.fromisoformat(frozen_at_raw)
                elapsed_min = (utcnow() - frozen_at).total_seconds() / 60.0
                if elapsed_min >= settings.breaker_cooldown_min:
                    runtime.unfreeze(db)
                    audit.log(db, "circuit", "rearm", detail={"elapsed_min": elapsed_min, **m})
                    db.commit()
            except ValueError:
                pass  # malformed timestamp — stay frozen

    return {
        "frozen": runtime.is_frozen(db),
        "reasons": reasons,
        "reason_codes": [code for code, _ in coded],
        "streak_rule": "legacy" if shadow else "grouped",
        **m,
    }


def reset(db: Session) -> dict:
    """Manual unfreeze — bypasses cooldown. Returns full runtime state."""
    runtime.unfreeze(db)
    audit.log(db, "circuit", "reset")
    db.commit()
    return runtime.state(db)
