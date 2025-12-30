"""
Advanced Paper Trading Execution Engine v0.3.0

Features:
- Partial order fills with remaining_qty tracking
- Fee and slippage modeling
- Enhanced trade reporting with cost breakdown
- Latency simulation (future)
- Stop-loss order automation

This module extends the v0.2.0 execution engine with realistic trading conditions.
"""

from datetime import datetime
from typing import Tuple, Dict, Any, List, Optional
import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from .config import ExecutionConfig, DEFAULT_CONFIG
from .paper_execution import Order, Trade, Position

logger = logging.getLogger(__name__)


class PartialFillResult:
    """Result of a partial fill operation."""
    
    def __init__(self):
        self.filled_qty: float = 0.0
        self.remaining_qty: float = 0.0
        self.original_qty: float = 0.0
        self.trades: List[Trade] = []
        self.total_fees: float = 0.0
        self.total_slippage: float = 0.0
        self.total_realized_pnl: float = 0.0
        self.effective_price: float = 0.0
        self.status: str = "PARTIAL"  # PARTIAL, FILLED
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "filled_qty": self.filled_qty,
            "remaining_qty": self.remaining_qty,
            "fill_ratio": self.filled_qty / self.original_qty if self.original_qty > 0 else 0.0,
            "effective_price": self.effective_price,
            "fees": self.total_fees,
            "slippage": self.total_slippage,
            "realized_pnl": self.total_realized_pnl,
            "trade_count": len(self.trades),
            "status": self.status,
        }


def calculate_partial_fill_qty(
    total_qty: float,
    config: ExecutionConfig,
) -> float:
    """
    Calculate how much of an order to fill.
    
    Args:
        total_qty: Total order quantity
        config: Execution configuration
    
    Returns:
        Quantity to fill in this iteration
    """
    fill_qty = config.partial_fill.get_fill_qty(total_qty)
    # Ensure we don't exceed total quantity
    return min(fill_qty, total_qty)


def apply_execution_costs(
    qty: float,
    price: float,
    side: str,
    config: ExecutionConfig,
) -> Tuple[float, float, float]:
    """
    Apply fees and slippage to an order execution.
    
    Args:
        qty: Order quantity
        price: Original price
        side: BUY or SELL
        config: Execution configuration
    
    Returns:
        Tuple of (effective_price, total_fees, slippage_amount)
    """
    # Apply slippage first (affects execution price)
    slipped_price, slippage_amount = config.slippage.apply_slippage(price, side)
    
    # Calculate notional value (for fee calculation)
    notional_value = qty * slipped_price
    
    # Calculate fees on the slipped price
    fees = config.fees.calculate_fee(notional_value, is_maker=False)
    
    # Effective price includes fees (spread them over quantity)
    fee_per_unit = fees / qty if qty > 0 else 0
    
    if side.upper() == "BUY":
        # Fees increase the effective cost for BUY
        effective_price = slipped_price + fee_per_unit
    else:
        # Fees decrease the proceeds for SELL
        effective_price = slipped_price - fee_per_unit
    
    return effective_price, fees, slippage_amount


def simulate_partial_fill(
    session: Session,
    order: Order,
    config: ExecutionConfig = DEFAULT_CONFIG,
) -> Tuple[bool, PartialFillResult]:
    """
    Simulate a partial order fill with fees and slippage.
    
    This function:
    1. Calculates fill quantity based on partial fill config
    2. Applies slippage and fees
    3. Updates position (with cost basis for multiple fills)
    4. Creates trade record with cost details
    5. Updates remaining_qty on order
    
    Args:
        session: SQLAlchemy session
        order: Order to fill (partially)
        config: Execution configuration
    
    Returns:
        Tuple of (success: bool, result: PartialFillResult)
    """
    result = PartialFillResult()
    
    if order.status == "FILLED":
        result.status = "FILLED"
        return False, result
    
    try:
        qty = float(order.qty)
        price = float(order.price)
        remaining = float(order.remaining_qty) if order.remaining_qty else qty
    except (ValueError, TypeError) as e:
        logger.error(f"Failed to convert numeric values for order {order.id}: {str(e)}")
        return False, result
    
    result.original_qty = qty
    
    # Calculate fill quantity
    fill_qty = calculate_partial_fill_qty(remaining, config)
    fill_qty = min(fill_qty, remaining)  # Don't exceed remaining
    
    if fill_qty <= 0:
        result.status = order.status
        return False, result
    
    # Apply execution costs
    effective_price, fees, slippage = apply_execution_costs(
        fill_qty, price, order.side, config
    )
    
    result.filled_qty = fill_qty
    result.remaining_qty = remaining - fill_qty
    result.total_fees = fees
    result.total_slippage = slippage
    result.effective_price = effective_price
    
    # Fetch or create position
    pos = session.query(Position).filter_by(symbol=order.symbol).one_or_none()
    
    # ============================================================
    # SELL ORDER: Position reduction and realized PnL
    # ============================================================
    if order.side == "SELL":
        if pos is None or float(pos.size) < fill_qty:
            current_size = float(pos.size) if pos else 0.0
            logger.error(
                f"Insufficient position for SELL: requested {fill_qty}, "
                f"current position {current_size} for {order.symbol}"
            )
            result.status = "FAILED"
            return False, result
        
        # Calculate realized PnL (cost basis - sale proceeds)
        old_avg = float(pos.avg_price)
        cost_basis = fill_qty * old_avg
        gross_pnl = (price - old_avg) * fill_qty
        realized_pnl = gross_pnl - fees  # Net of fees
        
        result.total_realized_pnl = realized_pnl
        
        # Create trade record with cost details
        trade = Trade(
            order_id=order.id,
            symbol=order.symbol,
            side="SELL",
            qty=fill_qty,
            price=float(effective_price),  # Use effective price in trade record
            ts=datetime.utcnow(),
        )
        
        # Update position
        new_size = float(pos.size) - fill_qty
        pos.size = Decimal(str(new_size))
        pos.realized_pnl = Decimal(str(float(pos.realized_pnl) + realized_pnl))
        pos.updated_at = datetime.utcnow()
    
    # ============================================================
    # BUY ORDER: Position creation/update with averaged cost
    # ============================================================
    else:  # BUY
        if pos is None:
            pos = Position(
                symbol=order.symbol,
                size=Decimal(str(fill_qty)),
                avg_price=Decimal(str(effective_price)),
                realized_pnl=Decimal("0.0"),
                updated_at=datetime.utcnow(),
            )
            session.add(pos)
        else:
            # Update average price
            old_size = float(pos.size)
            old_avg = float(pos.avg_price)
            new_size = old_size + fill_qty
            
            # Cost-weighted average
            total_cost = (old_size * old_avg) + (fill_qty * effective_price)
            new_avg = total_cost / new_size if new_size > 0 else 0
            
            pos.size = Decimal(str(new_size))
            pos.avg_price = Decimal(str(new_avg))
            pos.updated_at = datetime.utcnow()
        
        # Create trade record
        trade = Trade(
            order_id=order.id,
            symbol=order.symbol,
            side="BUY",
            qty=fill_qty,
            price=float(effective_price),
            ts=datetime.utcnow(),
        )
    
    session.add(trade)
    result.trades.append(trade)
    
    # Update order remaining_qty
    order.remaining_qty = Decimal(str(result.remaining_qty))
    
    # Update order status
    if result.remaining_qty <= 0:
        order.status = "FILLED"
        result.status = "FILLED"
    else:
        order.status = "PARTIAL"
        result.status = "PARTIAL"
    
    order.updated_at = datetime.utcnow()
    
    session.commit()
    logger.info(
        f"Filled {fill_qty}/{qty} of order {order.id} ({order.symbol}). "
        f"Fees: ${fees:.2f}, Slippage: ${slippage:.2f}"
    )
    
    return True, result


def simulate_full_fill_with_costs(
    session: Session,
    order: Order,
    config: ExecutionConfig = DEFAULT_CONFIG,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Wrapper that fills an order completely (with multiple partial fills if needed).
    
    Uses partial fill logic internally but continues until order is fully filled.
    
    Args:
        session: SQLAlchemy session
        order: Order to fill completely
        config: Execution configuration
    
    Returns:
        Tuple of (success: bool, summary: dict with aggregated metrics)
    """
    summary = {
        "order_id": order.id,
        "symbol": order.symbol,
        "side": order.side,
        "total_qty": float(order.qty),
        "total_filled": 0.0,
        "remaining": float(order.qty),
        "trades": [],
        "total_fees": 0.0,
        "total_slippage": 0.0,
        "total_realized_pnl": 0.0,
        "average_effective_price": 0.0,
        "status": "FILLED",
    }
    
    # Fill until order is complete or no more fills possible
    max_iterations = 100  # Safety limit
    iteration = 0
    tolerance = 1e-8  # Allow for floating point rounding errors
    
    while iteration < max_iterations:
        remaining = float(order.remaining_qty) if order.remaining_qty else float(order.qty)
        
        if remaining < tolerance:  # Order is effectively filled
            order.status = "FILLED"
            break
        
        success, result = simulate_partial_fill(session, order, config)
        
        if not success:
            break
        
        summary["total_filled"] += result.filled_qty
        summary["remaining"] = result.remaining_qty
        summary["total_fees"] += result.total_fees
        summary["total_slippage"] += result.total_slippage
        summary["total_realized_pnl"] += result.total_realized_pnl
        
        for trade in result.trades:
            summary["trades"].append({
                "trade_id": trade.id,
                "qty": trade.qty,
                "price": trade.price,
                "ts": trade.ts.isoformat() if trade.ts else None,
            })
        
        iteration += 1
        
        # Check if order is fully filled
        if order.status == "FILLED":
            break
    
    # Calculate average effective price
    if summary["total_filled"] > 0:
        total_notional = sum(
            float(t["qty"]) * float(t["price"]) for t in summary["trades"]
        )
        summary["average_effective_price"] = total_notional / summary["total_filled"]
    
    summary["status"] = order.status
    
    return order.status == "FILLED", summary
