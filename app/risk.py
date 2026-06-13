"""
Risk & pip sizing for FINDMY-FM (lean rebuild).

Two responsibilities:
1. Pip sizing — convert "pips" to exchange-valid order quantities.
2. Pre-queue risk checks — position-size and daily-loss limits.

Risk checks never BLOCK an order; they return violations that are attached as a
note to the pending order, so the user keeps final judgment at approval time.
"""

from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import portfolio
from app.config import settings
from app.market import get_exchange_info
from app.models import Fill, Position

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
    today = datetime.utcnow().date()
    start = datetime.combine(today, time.min)
    end = datetime.combine(today, time.max)
    total = (
        db.query(func.coalesce(func.sum(Fill.realized_pnl), 0.0))
        .filter(Fill.executed_at >= start, Fill.executed_at <= end, Fill.realized_pnl < 0)
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
