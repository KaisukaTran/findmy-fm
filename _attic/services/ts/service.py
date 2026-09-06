"""
TS Domain Service

Trade Service high-level business logic.
Orchestrates trade lifecycle, P&L calculations, and position tracking.
"""

from typing import Optional, Dict, List
from datetime import datetime
from sqlalchemy.orm import Session

from services.ts.db import SessionLocal
from services.ts import repository as ts_repo
from services.ts.models import Trade, TradePnL, TradePosition
from services.sot.models import Order


class TSService:
    """
    Trade Service (TS) - High-level trading domain operations.

    Responsibilities:
    - Own DB session for TS
    - Manage trade lifecycle (entry → exit)
    - Calculate P&L and performance metrics
    - Track positions and inventory
    - Expose domain-level operations
    - Hide DAL / ORM from API layer

    API layer MUST NOT touch DB or repository directly.
    """

    def __init__(self, db: Optional[Session] = None):
        self.db = db or SessionLocal()

    # ==================
    # Trade Lifecycle
    # ==================

    def open_trade(
        self,
        *,
        entry_order_id: int,
        symbol: str,
        side: str,
        entry_qty: float,
        entry_price: float,
        strategy_code: Optional[str] = None,
        signal_source: Optional[str] = None,
        requested_by: Optional[str] = None,
    ) -> int:
        """
        Open a new trade (entry order placed).
        
        Args:
            entry_order_id: SOT Order ID for entry
            symbol: Trading symbol (e.g., "AAPL")
            side: "BUY" or "SELL"
            entry_qty: Entry quantity
            entry_price: Entry price per unit
            strategy_code: Strategy identifier
            signal_source: Signal source (e.g., "manual", "backtest")
            requested_by: User/system that requested trade
            
        Returns:
            Trade ID
        """
        trade = ts_repo.TSRepository.create_trade(
            self.db,
            entry_order_id=entry_order_id,
            symbol=symbol,
            side=side,
            entry_qty=entry_qty,
            entry_price=entry_price,
            strategy_code=strategy_code,
            signal_source=signal_source,
            requested_by=requested_by,
        )
        
        # Initialize P&L snapshot
        cost_basis = entry_qty * entry_price
        ts_repo.TSRepository.create_or_update_trade_pnl(
            self.db,
            trade.id,
            gross_pnl=0.0,
            total_fees=0.0,
            cost_basis=cost_basis,
        )
        
        # Update position immediately after opening trade
        self._update_position(trade)
        
        self.db.commit()
        return trade.id

    def close_trade(
        self,
        trade_id: int,
        *,
        exit_order_id: int,
        exit_qty: float,
        exit_price: float,
    ) -> Dict:
        """
        Close or partially close a trade (exit order placed).
        
        Args:
            trade_id: Trade ID
            exit_order_id: SOT Order ID for exit
            exit_qty: Exit quantity
            exit_price: Exit price per unit
            
        Returns:
            Trade and P&L data
        """
        trade = ts_repo.TSRepository.close_trade(
            self.db,
            trade_id,
            exit_order_id=exit_order_id,
            exit_qty=exit_qty,
            exit_price=exit_price,
        )
        
        # Recalculate P&L
        pnl_data = self._calculate_trade_pnl(trade)
        
        ts_repo.TSRepository.create_or_update_trade_pnl(
            self.db,
            trade.id,
            **pnl_data,
        )
        
        # Update position
        self._update_position(trade)
        
        self.db.commit()
        
        return {
            "trade_id": trade.id,
            "status": trade.status,
            "pnl": pnl_data,
        }

    # ==================
    # P&L Calculations
    # ==================

    def _calculate_trade_pnl(self, trade: Trade) -> Dict:
        """
        Calculate P&L for a trade.
        
        Returns:
            Dict with gross_pnl, total_fees, cost_basis, etc.
        """
        # Cost basis
        cost_basis = trade.entry_qty * trade.entry_price
        
        # Entry fees (from SOT)
        entry_order = ts_repo.TSRepository.get_order_from_sot(self.db, trade.entry_order_id)
        entry_fees = 0.0
        if entry_order:
            entry_cost = ts_repo.TSRepository.get_order_cost_from_sot(
                self.db, trade.entry_order_id
            )
            entry_fees = entry_cost.total_fee if entry_cost else 0.0
        
        # Exit fees (from SOT, if trade is closed/partial)
        exit_fees = 0.0
        if trade.exit_order_id:
            exit_cost = ts_repo.TSRepository.get_order_cost_from_sot(
                self.db, trade.exit_order_id
            )
            exit_fees = exit_cost.total_fee if exit_cost else 0.0
        
        total_fees = entry_fees + exit_fees
        
        # P&L calculation
        if trade.exit_price and trade.exit_qty:
            gross_pnl = (trade.exit_price - trade.entry_price) * trade.exit_qty
            if trade.side == "SELL":
                # For SELL trades, profit when price goes down
                gross_pnl = (trade.entry_price - trade.exit_price) * trade.exit_qty
        else:
            gross_pnl = 0.0
        
        # Duration
        duration_minutes = None
        if trade.exit_time:
            delta = trade.exit_time - trade.entry_time
            duration_minutes = int(delta.total_seconds() / 60)
        
        # Calculate return percentage
        net_pnl = gross_pnl - total_fees
        return_pct = (net_pnl / cost_basis * 100) if cost_basis != 0 else 0.0
        
        return {
            "gross_pnl": gross_pnl,
            "total_fees": total_fees,
            "cost_basis": cost_basis,
            "entry_fees": entry_fees,
            "exit_fees": exit_fees,
            "net_pnl": net_pnl,
            "return_pct": return_pct,
            "realized_pnl": net_pnl if trade.status == "CLOSED" else 0.0,
            "unrealized_pnl": net_pnl if trade.status == "OPEN" else 0.0,
            "duration_minutes": duration_minutes,
        }

    def get_trade_pnl(self, trade_id: int) -> Dict:
        """Get P&L snapshot for a trade."""
        pnl = ts_repo.TSRepository.get_trade_pnl(self.db, trade_id)
        if not pnl:
            raise ValueError(f"P&L for trade {trade_id} not found")
        
        return {
            "trade_id": pnl.trade_id,
            "gross_pnl": pnl.gross_pnl,
            "total_fees": pnl.total_fees,
            "net_pnl": pnl.net_pnl,
            "return_pct": pnl.return_pct,
            "cost_basis": pnl.cost_basis,
            "realized_pnl": pnl.realized_pnl,
            "unrealized_pnl": pnl.unrealized_pnl,
            "duration_minutes": pnl.duration_minutes,
            "calculated_at": pnl.calculated_at.isoformat(),
        }

    def get_total_pnl(self) -> Dict:
        """Get total P&L across all trades."""
        total = ts_repo.TSRepository.get_total_pnl(self.db)
        
        return {
            "total_realized_pnl": total,
            "calculated_at": datetime.utcnow().isoformat(),
        }

    # ==================
    # Trade Queries
    # ==================

    def get_trade(self, trade_id: int) -> Dict:
        """Get trade details."""
        trade = ts_repo.TSRepository.get_trade(self.db, trade_id)
        if not trade:
            raise ValueError(f"Trade {trade_id} not found")
        
        pnl = ts_repo.TSRepository.get_trade_pnl(self.db, trade_id)
        
        return {
            "id": trade.id,
            "symbol": trade.symbol,
            "side": trade.side,
            "status": trade.status,
            "entry_qty": trade.entry_qty,
            "entry_price": trade.entry_price,
            "entry_time": trade.entry_time.isoformat(),
            "exit_qty": trade.exit_qty,
            "exit_price": trade.exit_price,
            "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
            "current_qty": trade.current_qty,
            "strategy_code": trade.strategy_code,
            "signal_source": trade.signal_source,
            "pnl": {
                "net_pnl": pnl.net_pnl if pnl else 0.0,
                "return_pct": pnl.return_pct if pnl else 0.0,
            } if pnl else None,
        }

    def list_trades(
        self,
        *,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        strategy_code: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict]:
        """List trades with optional filters."""
        trades = ts_repo.TSRepository.list_trades(
            self.db,
            symbol=symbol,
            status=status,
            strategy_code=strategy_code,
            limit=limit,
            offset=offset,
        )
        
        result = []
        for trade in trades:
            pnl = ts_repo.TSRepository.get_trade_pnl(self.db, trade.id)
            result.append({
                "id": trade.id,
                "symbol": trade.symbol,
                "side": trade.side,
                "status": trade.status,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "current_qty": trade.current_qty,
                "strategy_code": trade.strategy_code,
                "net_pnl": pnl.net_pnl if pnl else 0.0,
                "return_pct": pnl.return_pct if pnl else 0.0,
            })
        
        return result

    # ==================
    # Position Tracking
    # ==================

    def _update_position(self, trade: Trade) -> None:
        """Update position state after trade."""
        # Determine quantity change based on side
        qty_change = trade.entry_qty if trade.side == "BUY" else -trade.entry_qty
        
        pos = ts_repo.TSRepository.get_position(self.db, trade.symbol, trade.strategy_code)
        
        if not pos:
            # New position
            ts_repo.TSRepository.create_or_update_position(
                self.db,
                trade.symbol,
                quantity=qty_change,  # Buy is positive, sell is negative
                avg_entry_price=trade.entry_price,
                total_traded=trade.entry_qty,
                total_cost=trade.entry_qty * trade.entry_price,
                strategy_code=trade.strategy_code,
            )
        else:
            # Update existing position
            new_qty = pos.quantity + qty_change
            
            if new_qty > 0:
                # Still have long position - average the cost basis
                if trade.side == "BUY":
                    avg_price = (
                        (pos.quantity * pos.avg_entry_price + qty_change * trade.entry_price)
                        / new_qty
                    )
                else:
                    # SELL doesn't change avg_price
                    avg_price = pos.avg_entry_price
            elif new_qty < 0:
                # Have short position
                avg_price = trade.entry_price if abs(qty_change) > abs(pos.quantity) else pos.avg_entry_price
            else:
                # Position closed
                avg_price = 0.0
            
            ts_repo.TSRepository.create_or_update_position(
                self.db,
                trade.symbol,
                quantity=new_qty,
                avg_entry_price=avg_price,
                total_traded=pos.total_traded + trade.entry_qty,
                total_cost=pos.total_cost + (trade.entry_qty * trade.entry_price),
                strategy_code=trade.strategy_code,
            )

    def get_position(
        self,
        symbol: str,
        strategy_code: Optional[str] = None,
    ) -> Dict:
        """Get current position for a symbol."""
        pos = ts_repo.TSRepository.get_position(self.db, symbol, strategy_code)
        if not pos:
            return {
                "symbol": symbol,
                "quantity": 0.0,
                "avg_entry_price": 0.0,
                "total_traded": 0.0,
                "total_cost": 0.0,
            }
        
        return {
            "symbol": pos.symbol,
            "quantity": pos.quantity,
            "avg_entry_price": pos.avg_entry_price,
            "total_traded": pos.total_traded,
            "total_cost": pos.total_cost,
            "strategy_code": pos.strategy_code,
            "last_trade_time": pos.last_trade_time.isoformat() if pos.last_trade_time else None,
        }

    def list_positions(self) -> List[Dict]:
        """List all open positions."""
        positions = ts_repo.TSRepository.list_positions(self.db)
        
        return [
            {
                "symbol": pos.symbol,
                "quantity": pos.quantity,
                "avg_entry_price": pos.avg_entry_price,
                "total_traded": pos.total_traded,
                "total_cost": pos.total_cost,
                "strategy_code": pos.strategy_code,
            }
            for pos in positions
        ]

    # ==================
    # Housekeeping
    # ==================

    def close(self):
        """Close database session."""
        self.db.close()
