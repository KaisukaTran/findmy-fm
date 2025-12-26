"""
TS Repository Layer

Provides data access operations for Trade Service.
Integrates with SOT to read order data.
"""

from typing import Optional, List
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from services.ts.models import Trade, TradePnL, TradePosition, TradePerformance
from services.sot.models import Order, OrderCost


class TSRepository:
    """
    Trade Service Repository
    Handles all database operations for TS domain.
    """

    # ==================
    # Trade Operations
    # ==================

    @staticmethod
    def create_trade(
        db: Session,
        *,
        entry_order_id: int,
        symbol: str,
        side: str,
        entry_qty: float,
        entry_price: float,
        strategy_code: Optional[str] = None,
        signal_source: Optional[str] = None,
        requested_by: Optional[str] = None,
    ) -> Trade:
        """Create a new trade (entry order placed)."""
        trade = Trade(
            entry_order_id=entry_order_id,
            symbol=symbol,
            side=side,
            status="OPEN",
            entry_qty=entry_qty,
            entry_price=entry_price,
            entry_time=datetime.utcnow(),
            current_qty=entry_qty,
            current_price=entry_price,
            strategy_code=strategy_code,
            signal_source=signal_source,
            requested_by=requested_by,
        )
        db.add(trade)
        db.flush()
        return trade

    @staticmethod
    def close_trade(
        db: Session,
        trade_id: int,
        *,
        exit_order_id: int,
        exit_qty: float,
        exit_price: float,
    ) -> Trade:
        """Close or partially close a trade."""
        trade = db.get(Trade, trade_id)
        if not trade:
            raise ValueError(f"Trade {trade_id} not found")

        trade.exit_order_id = exit_order_id
        trade.exit_qty = exit_qty
        trade.exit_price = exit_price
        trade.exit_time = datetime.utcnow()
        
        # Update status
        remaining_qty = trade.current_qty - exit_qty
        if remaining_qty > 0:
            trade.status = "PARTIAL"
            trade.current_qty = remaining_qty
        else:
            trade.status = "CLOSED"
            trade.current_qty = 0.0

        db.flush()
        return trade

    @staticmethod
    def get_trade(db: Session, trade_id: int) -> Optional[Trade]:
        """Retrieve a trade by ID."""
        return db.get(Trade, trade_id)

    @staticmethod
    def list_trades(
        db: Session,
        *,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        strategy_code: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Trade]:
        """List trades with optional filters."""
        query = db.query(Trade)
        
        if symbol:
            query = query.filter(Trade.symbol == symbol)
        if status:
            query = query.filter(Trade.status == status)
        if strategy_code:
            query = query.filter(Trade.strategy_code == strategy_code)
        
        return query.order_by(Trade.created_at.desc()).limit(limit).offset(offset).all()

    # ==================
    # P&L Operations
    # ==================

    @staticmethod
    def create_or_update_trade_pnl(
        db: Session,
        trade_id: int,
        *,
        gross_pnl: float,
        total_fees: float,
        cost_basis: float,
        realized_pnl: float = 0.0,
        unrealized_pnl: float = 0.0,
        return_pct: Optional[float] = None,
        duration_minutes: Optional[int] = None,
        entry_fees: Optional[float] = None,
        exit_fees: Optional[float] = None,
        net_pnl: Optional[float] = None,
    ) -> TradePnL:
        """Create or update P&L snapshot for a trade."""
        pnl = db.query(TradePnL).filter(TradePnL.trade_id == trade_id).first()
        
        if not pnl:
            pnl = TradePnL(trade_id=trade_id)
            db.add(pnl)
        
        pnl.gross_pnl = gross_pnl
        pnl.total_fees = total_fees
        pnl.net_pnl = net_pnl if net_pnl is not None else (gross_pnl - total_fees)
        pnl.cost_basis = cost_basis
        pnl.realized_pnl = realized_pnl
        pnl.unrealized_pnl = unrealized_pnl
        
        if entry_fees is not None:
            pnl.entry_fees = entry_fees
        if exit_fees is not None:
            pnl.exit_fees = exit_fees
        
        # Calculate return % if not provided
        if return_pct is not None:
            pnl.return_pct = return_pct
        elif cost_basis != 0:
            pnl.return_pct = (pnl.net_pnl / cost_basis) * 100
        else:
            pnl.return_pct = 0.0
        
        if duration_minutes is not None:
            pnl.duration_minutes = duration_minutes
        
        db.flush()
        return pnl

    @staticmethod
    def get_trade_pnl(db: Session, trade_id: int) -> Optional[TradePnL]:
        """Get P&L snapshot for a trade."""
        return db.query(TradePnL).filter(TradePnL.trade_id == trade_id).first()

    @staticmethod
    def get_total_pnl(db: Session) -> float:
        """Get total realized P&L across all closed trades."""
        result = db.query(func.sum(TradePnL.net_pnl)).filter(
            Trade.status == "CLOSED"
        ).scalar()
        return result or 0.0

    # ==================
    # Position Operations
    # ==================

    @staticmethod
    def create_or_update_position(
        db: Session,
        symbol: str,
        *,
        quantity: float,
        avg_entry_price: float,
        total_traded: float,
        total_cost: float,
        strategy_code: Optional[str] = None,
    ) -> TradePosition:
        """Create or update position for a symbol."""
        pos = db.query(TradePosition).filter(
            and_(
                TradePosition.symbol == symbol,
                TradePosition.strategy_code == strategy_code,
            )
        ).first()
        
        if not pos:
            pos = TradePosition(symbol=symbol, strategy_code=strategy_code)
            db.add(pos)
        
        pos.quantity = quantity
        pos.avg_entry_price = avg_entry_price
        pos.total_traded = total_traded
        pos.total_cost = total_cost
        pos.last_trade_time = datetime.utcnow()
        
        db.flush()
        return pos

    @staticmethod
    def get_position(
        db: Session,
        symbol: str,
        strategy_code: Optional[str] = None,
    ) -> Optional[TradePosition]:
        """Get current position for a symbol."""
        query = db.query(TradePosition).filter(TradePosition.symbol == symbol)
        if strategy_code:
            query = query.filter(TradePosition.strategy_code == strategy_code)
        return query.first()

    @staticmethod
    def list_positions(db: Session) -> List[TradePosition]:
        """List all open positions."""
        return db.query(TradePosition).filter(
            TradePosition.quantity != 0
        ).all()

    # ==================
    # Performance Analytics
    # ==================

    @staticmethod
    def create_performance_bucket(
        db: Session,
        *,
        bucket_time: datetime,
        bucket_type: str,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        breakeven_trades: int,
        total_pnl: float,
        net_pnl: float,
        total_fees: float,
        avg_win: Optional[float] = None,
        avg_loss: Optional[float] = None,
    ) -> TradePerformance:
        """Create a performance bucket (hourly/daily/weekly)."""
        perf = TradePerformance(
            bucket_time=bucket_time,
            bucket_type=bucket_type,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            breakeven_trades=breakeven_trades,
            total_pnl=total_pnl,
            net_pnl=net_pnl,
            total_fees=total_fees,
            avg_win=avg_win,
            avg_loss=avg_loss,
        )
        
        # Calculate metrics
        if total_trades > 0:
            perf.win_rate = (winning_trades / total_trades) * 100
            perf.avg_pnl = net_pnl / total_trades
        
        db.add(perf)
        db.flush()
        return perf

    @staticmethod
    def get_daily_performance(
        db: Session,
        symbol: Optional[str] = None,
    ) -> List[TradePerformance]:
        """Get daily performance aggregation."""
        query = db.query(TradePerformance).filter(
            TradePerformance.bucket_type == "daily"
        )
        return query.order_by(TradePerformance.bucket_time.desc()).all()

    # ==================
    # SOT Integration
    # ==================

    @staticmethod
    def get_order_from_sot(db: Session, order_id: int) -> Optional[Order]:
        """Fetch order data from SOT."""
        return db.get(Order, order_id)

    @staticmethod
    def get_order_pnl_from_sot(db: Session, order_id: int) -> Optional[OrderCost]:
        """Fetch order cost from SOT."""
        return db.get(OrderCost, order_id)

    @staticmethod
    def get_order_cost_from_sot(db: Session, order_id: int) -> Optional[OrderCost]:
        """Fetch order cost from SOT."""
        return db.get(OrderCost, order_id)
