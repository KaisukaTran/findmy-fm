"""
Deposit ledger — the mirror-image, append-only counterpart to app/costs.py's Withdrawal
ledger. The owner tops up $500-1,000 of fresh capital most months; this is the one place that
gets recorded, and everything else (``risk.capital_anchor``, the capital-scale sizing anchor,
ROI, the drawdown baseline) reads from it — there is no separate "apply" step.

Recording a deposit does three things, in order:
  1. Validates and inserts an append-only ``Deposit`` fact (never edited after insert).
  2. Audits ``deposit_recorded``.
  3. Forces ``capital_scale``'s percentage-of-equity anchor to re-adopt the new equity NOW,
     bypassing its usual 10% deadband once (see ``_reanchor_capital_scale`` below) — a deposit
     is a real capital event the operator just told us about, not the mark-to-market P&L noise
     the deadband exists to ignore (app/capital_scale.py's module docstring, "THE TRAP").

Idempotency: an identical (amount, note) pair recorded again inside ``_DUPLICATE_WINDOW_SEC``
of the previous one is rejected — a defence against a double-click or a retried request landing
twice, independent of any client-side disabling of the submit button.
"""

from __future__ import annotations

import math

from sqlalchemy.orm import Session

from app import audit, runtime
from app.clock import utcnow
from app.models import Deposit

MAX_DEPOSIT_USD = 10_000_000.0
_DUPLICATE_WINDOW_SEC = 10.0


def _is_duplicate(db: Session, amount: float, note: str | None) -> bool:
    """True when the most recently recorded deposit matches (amount, note) and landed within
    the duplicate window — the double-submit guard."""
    last = db.query(Deposit).order_by(Deposit.id.desc()).first()
    if last is None:
        return False
    if last.amount != amount or (last.note or None) != (note or None):
        return False
    age = (utcnow() - last.created_at).total_seconds() if last.created_at else float("inf")
    return age < _DUPLICATE_WINDOW_SEC


def record_deposit(db: Session, amount: float, note: str | None = None) -> Deposit:
    """Book a deposit: validate, reject a duplicate double-submit, persist, audit, and
    re-anchor capital_scale's equity anchor immediately (see module docstring)."""
    if amount is None or not math.isfinite(amount) or amount <= 0:
        raise ValueError("amount must be a positive, finite number")
    if amount > MAX_DEPOSIT_USD:
        raise ValueError(f"amount exceeds the maximum of {MAX_DEPOSIT_USD:,.0f} USD")
    note = (note or "").strip()[:200] or None
    if _is_duplicate(db, amount, note):
        raise ValueError("duplicate deposit: identical amount/note recorded within 10s")

    # Snapshot true mark-to-market equity BEFORE this deposit lands — the same number
    # portfolio.summary_view reports as `total_equity` (cash + open-position market value,
    # including any unrealized P&L). `portfolio._nav_walk` prices this deposit's units off it
    # instead of a realized-only running total, so a deposit made while a position is
    # underwater (SL=0 means that loss is almost always unrealized) can never dilute the real
    # drawdown the circuit breaker reads.
    from app import portfolio  # lazy: avoid a portfolio <-> deposits import cycle

    equity_before = portfolio.summary_view(db)["total_equity"]

    d = Deposit(amount=float(amount), note=note, equity_before=equity_before)
    db.add(d)
    db.commit()
    db.refresh(d)
    audit.log(db, "deposits", "deposit_recorded", entity=str(d.id), amount=amount, note=note or "")
    db.commit()
    _reanchor_capital_scale(db)
    return d


def _reanchor_capital_scale(db: Session) -> None:
    """Force capital_scale.anchored_equity's stored anchor to the fresh equity reading.

    Bypasses the usual deadband ONCE, on purpose: capital_scale.py's module docstring reserves
    that deadband for mark-to-market P&L noise, not a real capital event the operator just told
    us about (a $500 deposit on a $7,000 book is a 7% move — inside the default 10% deadband,
    so the scaled first wave would otherwise silently ignore it for however long it takes P&L
    to drift the rest of the way). Kept out of app/capital_scale.py itself so that module's own
    guarantee — ``test_never_calls_runtime_set_with_any_key_but_the_anchor`` — stays a statement
    about capital_scale's OWN call paths, not this one.
    """
    from app import risk  # lazy: avoid a risk <-> deposits import cycle

    live = risk.account_equity(db)
    old = runtime.get(db, runtime.KEY_CAPITAL_SCALE_ANCHOR)
    runtime.set(db, runtime.KEY_CAPITAL_SCALE_ANCHOR, live)
    audit.log(db, "capital_scale", "anchor_moved", old=old, new=round(live, 2), reason="deposit")
    db.commit()


def list_deposits(db: Session, limit: int = 50) -> list[Deposit]:
    return db.query(Deposit).order_by(Deposit.id.desc()).limit(limit).all()
