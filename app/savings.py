"""
Savings / long-term holdings ledger (src='KAI').

Coins the operator holds as savings (bought externally) and records here for protection +
display. Deliberately SEPARATE from the trading `Position` table: the orphan manager, scanner
and OPUS only ever read Position + KssSession, so a savings holding is structurally invisible
to every auto-sell path — the bot can never sell it by mistake. The bot may still trade the
same symbol with its own capital (that inventory is the Position row, independent of this).

Write surface: add_holding / set_holding / remove_holding. Read: list_holdings (priced live).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import SavingsHolding


def _get(db: Session, symbol: str) -> SavingsHolding | None:
    return db.query(SavingsHolding).filter(SavingsHolding.symbol == symbol).one_or_none()


def add_holding(
    db: Session, symbol: str, quantity: float, avg_cost: float, note: str | None = None
) -> SavingsHolding:
    """Accumulate a savings buy. Existing symbol → qty += and a cost-weighted avg; new → create.
    `avg_cost` is the USD price/unit of THIS buy."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise ValueError("symbol required")
    if quantity is None or quantity <= 0:
        raise ValueError("quantity must be positive")
    if avg_cost is None or avg_cost < 0:
        raise ValueError("avg_cost must be >= 0")

    h = _get(db, symbol)
    if h is None:
        h = SavingsHolding(symbol=symbol, quantity=float(quantity), avg_cost=float(avg_cost),
                           src="KAI", note=note or None)
        db.add(h)
    else:
        new_qty = h.quantity + quantity
        # cost-weighted average so the blended basis stays correct as buys accumulate
        h.avg_cost = (h.quantity * h.avg_cost + quantity * avg_cost) / new_qty if new_qty > 0 else 0.0
        h.quantity = new_qty
        if note:
            h.note = note
    db.commit()
    db.refresh(h)
    return h


def set_holding(
    db: Session, symbol: str, quantity: float, avg_cost: float, note: str | None = None
) -> SavingsHolding:
    """Overwrite a symbol's savings holding outright (manual correction/edit)."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise ValueError("symbol required")
    if quantity is None or quantity < 0:
        raise ValueError("quantity must be >= 0")
    h = _get(db, symbol)
    if h is None:
        h = SavingsHolding(symbol=symbol, src="KAI")
        db.add(h)
    h.quantity = float(quantity)
    h.avg_cost = float(avg_cost or 0.0)
    h.note = note or None
    db.commit()
    db.refresh(h)
    return h


def remove_holding(db: Session, symbol: str) -> bool:
    """Delete a savings holding. Returns True if a row was removed."""
    h = _get(db, (symbol or "").strip().upper())
    if h is None:
        return False
    db.delete(h)
    db.commit()
    return True


def list_holdings(db: Session) -> list[dict]:
    """All savings holdings, priced at the live market — each with value + unrealized PnL."""
    rows = db.query(SavingsHolding).order_by(SavingsHolding.symbol).all()
    if not rows:
        return []
    from app.market import get_current_prices

    prices = get_current_prices([r.symbol for r in rows])
    out = []
    for r in rows:
        px = prices.get(r.symbol) or 0.0
        cost = r.quantity * r.avg_cost
        value = r.quantity * px
        pnl = value - cost if px > 0 else 0.0
        out.append({
            **r.to_dict(),
            "price": px,
            "value": value,
            "unrealized_pnl": pnl,
            "unrealized_pnl_pct": (pnl / cost * 100) if cost > 0 else 0.0,
        })
    return out


def summary(db: Session) -> dict:
    """Totals across all savings holdings (cost basis + live value + PnL)."""
    rows = list_holdings(db)
    cost = sum(r["cost_basis"] for r in rows)
    value = sum(r["value"] for r in rows)
    return {
        "count": len(rows),
        "cost_basis": cost,
        "value": value,
        "unrealized_pnl": value - cost,
        "rows": rows,
    }
