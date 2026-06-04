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
from app.config import settings
from app.models import Fill

# Reviewers that must be blocked when the breaker is frozen.
AUTO_REVIEWERS: frozenset[str] = frozenset({"auto-trader", "auto-approver", "scheduler"})


def _consecutive_losses(db: Session) -> int:
    """Count leading SELL fills with realized_pnl < 0, most-recent first."""
    fills = (
        db.query(Fill)
        .filter(Fill.side == "SELL")
        .order_by(Fill.executed_at.desc())
        .limit(20)
        .all()
    )
    count = 0
    for f in fills:
        if f.realized_pnl < 0:
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
        "drawdown_pct": perf["max_drawdown_pct"],
        "daily_loss_pct": dl / eq * 100,
        "consecutive_losses": _consecutive_losses(db),
    }


def evaluate(db: Session) -> dict:
    """Evaluate breaker thresholds; trip freeze or auto-rearm as needed.

    Safe to call every scheduler cycle — idempotent when state is stable.
    """
    m = metrics(db)
    reasons: list[str] = []

    if m["drawdown_pct"] > settings.max_drawdown_pct:
        reasons.append(
            f"drawdown {m['drawdown_pct']:.1f}% > limit {settings.max_drawdown_pct}%"
        )
    if m["daily_loss_pct"] > settings.daily_loss_hard_pct:
        reasons.append(
            f"daily_loss {m['daily_loss_pct']:.1f}% > limit {settings.daily_loss_hard_pct}%"
        )
    if m["consecutive_losses"] >= settings.max_consecutive_losses:
        reasons.append(
            f"consecutive_losses {m['consecutive_losses']} >= limit {settings.max_consecutive_losses}"
        )

    currently_frozen = runtime.is_frozen(db)

    if reasons and not currently_frozen:
        reason_str = "; ".join(reasons)
        runtime.freeze(db, reason_str)
        audit.log(db, "circuit", "freeze", detail={"reasons": reasons, **m})
        db.commit()

    elif currently_frozen and not reasons:
        # Attempt auto-rearm only after cooldown has elapsed.
        frozen_at_raw = runtime.get(db, runtime.KEY_FROZEN_AT)
        if frozen_at_raw:
            try:
                frozen_at = datetime.fromisoformat(frozen_at_raw)
                elapsed_min = (datetime.utcnow() - frozen_at).total_seconds() / 60.0
                if elapsed_min >= settings.breaker_cooldown_min:
                    runtime.unfreeze(db)
                    audit.log(db, "circuit", "rearm", detail={"elapsed_min": elapsed_min, **m})
                    db.commit()
            except ValueError:
                pass  # malformed timestamp — stay frozen

    return {"frozen": runtime.is_frozen(db), "reasons": reasons, **m}


def reset(db: Session) -> dict:
    """Manual unfreeze — bypasses cooldown. Returns full runtime state."""
    runtime.unfreeze(db)
    audit.log(db, "circuit", "reset")
    db.commit()
    return runtime.state(db)
