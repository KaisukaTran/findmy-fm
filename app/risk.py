"""
Risk & pip sizing for FINDMY-FM (lean rebuild).

Two responsibilities:
1. Pip sizing — convert "pips" to exchange-valid order quantities.
2. Pre-queue risk checks — position-size and daily-loss limits.

Risk checks never BLOCK an order; they return violations that are attached as a
note to the pending order, so the user keeps final judgment at approval time.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, time, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit, portfolio
from app.clock import utcnow
from app.config import settings
from app.market import get_exchange_info
from app.models import Fill, Position, Withdrawal

logger = logging.getLogger(__name__)

# --- pip sizing ---------------------------------------------------------


def calculate_order_qty(symbol: str, pips: float = 1.0) -> float:
    """qty = pips × pip_multiplier × minQty, rounded to stepSize, floored at minQty."""
    info = get_exchange_info(symbol)
    min_qty = info.get("minQty", 0.00001)
    step = info.get("stepSize", 0.00001) or 0.00001
    qty = pips * settings.pip_multiplier * min_qty
    qty = round(qty / step) * step
    return max(qty, min_qty)


def validate_order_qty(symbol: str, quantity: float) -> tuple[bool, str]:
    """Validate a quantity against exchange min/max/step. Returns (ok, message)."""
    info = get_exchange_info(symbol)
    min_qty = info.get("minQty", 0.00001)
    max_qty = info.get("maxQty", 10000.0)
    step = info.get("stepSize", 0.00001) or 0.00001
    if quantity < min_qty:
        return False, f"Quantity {quantity} below minimum {min_qty}"
    if quantity > max_qty:
        return False, f"Quantity {quantity} exceeds maximum {max_qty}"
    if abs(quantity / step - round(quantity / step)) > 1e-9:
        return False, f"Quantity {quantity} not aligned with step size {step}"
    return True, ""


# --- capital anchor (Phase 0, docs/capital-scaling-2026-08-23.md §2.1) --------------------

_ANCHOR_CACHE_TTL_SEC = 60.0  # short TTL: a 30-min scan cycle / 90s guard must never hammer
# the exchange, but a stale balance must never live long either.
_anchor_cache: dict[str, float] = {}  # {"value": ..., "ts": ...} — process-wide, deliberately
# module-level (not per-request) so every caller in a scan cycle shares one fetch.
_anchor_fetch_warned = False  # True once the current failure episode has been audited; reset
# to False on the next successful fetch so a NEW outage is audited again (not silenced forever).


def _total_withdrawn(db: Session) -> float:
    """Cumulative amount actually withdrawn off the exchange (``Withdrawal.amount`` only).

    ``fee``/``vat`` are deliberately NOT included: it is unverified whether they are debited
    from the SAME exchange quote balance (vs. an external/tax ledger), so subtracting them
    could over-correct the anchor. ``amount`` — the principal that left the exchange — is
    unambiguous.
    """
    total = db.query(func.coalesce(func.sum(Withdrawal.amount), 0.0)).scalar()
    return float(total or 0.0)


def capital_anchor(db: Session) -> float:
    """The capital base every capital-derived size (equity, position caps, ...) is computed
    from — replacing the bare ``settings.account_equity`` constant.

    - Paper (``live_trading=False``): returns ``settings.account_equity`` exactly, always —
      byte-identical to pre-Phase-0 behaviour. Withdrawals are a real-money/live concept only.
    - Live, ``use_exchange_balance`` off (default): ``settings.account_equity`` minus
      cumulative real withdrawals (the constant never accounted for money that actually left
      the exchange).
    - Live, ``use_exchange_balance`` on: the REAL exchange quote-currency balance (free+used)
      via ccxt ``fetch_balance()`` — already nets out withdrawals, so none are subtracted again.
      Cached for ``_ANCHOR_CACHE_TTL_SEC``. Any fetch failure fails SOFT back to
      ``settings.account_equity`` (unadjusted — the exchange is unreachable, so we cannot know
      withdrawals against it either), logs a warning, and audits once per failure episode.
    """
    global _anchor_fetch_warned
    if not settings.live_trading:
        return settings.account_equity
    if not settings.use_exchange_balance:
        return settings.account_equity - _total_withdrawn(db)

    now = _time.time()
    cached_ts = _anchor_cache.get("ts")
    if cached_ts is not None and (now - cached_ts) < _ANCHOR_CACHE_TTL_SEC:
        return _anchor_cache["value"]

    try:
        from app.data.providers import live_provider  # local: avoid a module-load cycle
        from app.execution import fetch_account_balance  # local: live-only dependency

        quote = live_provider().quote
        balance = fetch_account_balance(quote)
        _anchor_cache["value"] = balance
        _anchor_cache["ts"] = now
        _anchor_fetch_warned = False
        return balance
    except Exception as exc:
        if not _anchor_fetch_warned:
            logger.warning("capital_anchor: fetch_balance failed, falling back to account_equity: %s", exc)
            # Audit WITHOUT committing: this runs inside read paths (portfolio.equity ->
            # scanner._can_open, the 90s guard), and committing here would flush whatever
            # half-built state the caller happens to hold. The row lands on the caller's next
            # commit; the warning above is the durable record either way.
            audit.log(db, "risk", "capital_anchor_fetch_failed", error=str(exc))
            _anchor_fetch_warned = True
        return settings.account_equity


def reset_capital_anchor_cache() -> None:
    """Clear the cached exchange balance (tests / an operator-triggered refresh)."""
    _anchor_cache.clear()


# --- risk checks --------------------------------------------------------


def account_equity(db: Session) -> float:
    """Live mark-to-market equity; falls back to config value if the book is empty."""
    live = portfolio.equity(db)
    return live if live > 0 else settings.account_equity


def current_exposure(symbol: str, db: Session) -> tuple[float, float]:
    """Return (quantity, exposure_pct) for the symbol's open position."""
    pos = db.query(Position).filter(Position.symbol == symbol).one_or_none()
    if not pos or pos.quantity <= 0:
        return 0.0, 0.0
    equity = account_equity(db)
    exposure_pct = (pos.total_cost / equity * 100) if equity > 0 else 0.0
    return pos.quantity, exposure_pct


def daily_loss(db: Session) -> float:
    """Sum of realized losses (positive number) from fills executed today (UTC)."""
    today = utcnow().date()
    start = datetime.combine(today, time.min)
    end = datetime.combine(today, time.max)
    total = (
        db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0))
        .filter(Fill.executed_at >= start, Fill.executed_at <= end, Fill.realized_pnl < 0)
        .scalar()
    )
    return abs(float(total or 0.0))


def weekly_loss(db: Session) -> float:
    """Sum of realized losses (positive number) from fills in the rolling last 7 days."""
    start = utcnow() - timedelta(days=7)
    total = (
        db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0))
        .filter(Fill.executed_at >= start, Fill.realized_pnl < 0)
        .scalar()
    )
    return abs(float(total or 0.0))


def check_position_size(symbol: str, qty: float, price: float, db: Session) -> str | None:
    """Return a violation string if adding qty@price would breach the position limit."""
    equity = account_equity(db)
    if equity <= 0:
        return None
    _, _ = current_exposure(symbol, db)
    pos = db.query(Position).filter(Position.symbol == symbol).one_or_none()
    current_cost = pos.total_cost if pos else 0.0
    new_cost = current_cost + qty * price
    new_pct = new_cost / equity * 100
    if new_pct > settings.max_position_size_pct:
        return f"Position size {new_pct:.1f}% exceeds max {settings.max_position_size_pct:.1f}%"
    return None


def check_daily_loss(db: Session) -> str | None:
    """Return a violation string if today's realized loss exceeds the daily limit."""
    equity = account_equity(db)
    if equity <= 0:
        return None
    loss_pct = daily_loss(db) / equity * 100
    if loss_pct > settings.max_daily_loss_pct:
        return f"Daily loss {loss_pct:.1f}% exceeds max {settings.max_daily_loss_pct:.1f}%"
    return None


def check_all_risks(
    symbol: str, qty: float, price: float, db: Session, side: str = "BUY"
) -> tuple[bool, list[str]]:
    """
    Run all pre-queue risk checks. Returns (passed, [violations]).

    These are ENTRY gates (they cap new exposure / halt on a loss spiral). A SELL *reduces*
    exposure, so it is never blocked — applying an "exceeds max position size" check to an
    exit would deadlock an oversized position (can't sell because it's too big → stays big).
    """
    if side.upper() == "SELL":
        return True, []
    violations: list[str] = []
    if v := check_position_size(symbol, qty, price, db):
        violations.append(v)
    if v := check_daily_loss(db):
        violations.append(v)
    return len(violations) == 0, violations
