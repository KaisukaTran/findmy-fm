"""
TS Service Tests

Comprehensive test suite for Trade Service functionality.
Tests trade lifecycle, P&L calculations, position tracking, and SOT integration.
"""

import pytest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from services.ts.models import Trade, TradePnL, TradePosition, TradePerformance
from services.ts.service import TSService
from services.ts import repository as ts_repo
from services.sot.models import Order, OrderCost, OrderFill
from services.sot.db import Base as SOT_Base
from services.ts.db import Base as TS_Base


# ==================
# Fixtures
# ==================

@pytest.fixture
def test_db():
    """Create in-memory SQLite database for testing."""
    # Create engine
    engine = create_engine("sqlite:///:memory:")
    
    # Create all tables
    SOT_Base.metadata.create_all(engine)
    TS_Base.metadata.create_all(engine)
    
    # Create session
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    
    yield db
    
    db.close()


@pytest.fixture
def ts_service(test_db):
    """Create TS service instance."""
    return TSService(test_db)


@pytest.fixture
def sample_order(test_db):
    """Create a sample order in SOT."""
    order = Order(
        order_request_id=1,
        exchange="NYSE",
        exchange_order_id="ORD-001",
        client_order_id="CLI-001",
        status="FILLED",
        avg_price=150.50,
        executed_qty=100,
    )
    test_db.add(order)
    test_db.commit()
    return order


@pytest.fixture
def sample_exit_order(test_db):
    """Create a sample exit order."""
    order = Order(
        order_request_id=2,
        exchange="NYSE",
        exchange_order_id="ORD-002",
        client_order_id="CLI-002",
        status="FILLED",
        avg_price=152.00,
        executed_qty=100,
    )
    test_db.add(order)
    test_db.commit()
    return order


# ==================
# Trade Lifecycle Tests
# ==================

class TestTradeLifecycle:
    """Test trade opening, closing, and state transitions."""

    def test_open_trade(self, ts_service, sample_order, test_db):
        """Test opening a new trade."""
        trade_id = ts_service.open_trade(
            entry_order_id=sample_order.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=100,
            entry_price=150.50,
            strategy_code="momentum_001",
        )
        
        assert trade_id > 0
        
        # Verify trade was created
        trade = ts_repo.TSRepository.get_trade(test_db, trade_id)
        assert trade is not None
        assert trade.symbol == "AAPL"
        assert trade.status == "OPEN"
        assert trade.entry_qty == 100
        assert trade.entry_price == 150.50
        assert trade.current_qty == 100

    def test_close_trade(self, ts_service, sample_order, sample_exit_order, test_db):
        """Test closing a trade."""
        # Open trade
        trade_id = ts_service.open_trade(
            entry_order_id=sample_order.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=100,
            entry_price=150.50,
        )
        
        # Close trade
        result = ts_service.close_trade(
            trade_id,
            exit_order_id=sample_exit_order.id,
            exit_qty=100,
            exit_price=152.00,
        )
        
        assert result["status"] == "CLOSED"
        assert result["pnl"]["gross_pnl"] == 150.0  # (152 - 150.50) * 100
        
        # Verify trade state
        trade = ts_repo.TSRepository.get_trade(test_db, trade_id)
        assert trade.status == "CLOSED"
        assert trade.exit_qty == 100
        assert trade.exit_price == 152.00

    def test_partial_close(self, ts_service, sample_order, sample_exit_order, test_db):
        """Test partial trade closure."""
        # Open trade with 200 qty
        trade_id = ts_service.open_trade(
            entry_order_id=sample_order.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=200,
            entry_price=150.50,
        )
        
        # Close 100 qty (partial)
        result = ts_service.close_trade(
            trade_id,
            exit_order_id=sample_exit_order.id,
            exit_qty=100,
            exit_price=152.00,
        )
        
        assert result["status"] == "PARTIAL"
        
        # Verify remaining qty
        trade = ts_repo.TSRepository.get_trade(test_db, trade_id)
        assert trade.current_qty == 100


# ==================
# P&L Calculation Tests
# ==================

class TestPnLCalculations:
    """Test P&L calculations including fees and returns."""

    def test_buy_pnl_positive(self, ts_service, sample_order, sample_exit_order, test_db):
        """Test positive P&L on BUY trade."""
        trade_id = ts_service.open_trade(
            entry_order_id=sample_order.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=100,
            entry_price=150.00,
        )
        
        ts_service.close_trade(
            trade_id,
            exit_order_id=sample_exit_order.id,
            exit_qty=100,
            exit_price=160.00,
        )
        
        pnl = ts_service.get_trade_pnl(trade_id)
        
        assert pnl["gross_pnl"] == 1000.0  # (160 - 150) * 100
        assert pnl["return_pct"] == (1000 / 15000) * 100  # net_pnl / cost_basis

    def test_buy_pnl_negative(self, ts_service, sample_order, sample_exit_order, test_db):
        """Test negative P&L on BUY trade."""
        trade_id = ts_service.open_trade(
            entry_order_id=sample_order.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=100,
            entry_price=150.00,
        )
        
        ts_service.close_trade(
            trade_id,
            exit_order_id=sample_exit_order.id,
            exit_qty=100,
            exit_price=140.00,
        )
        
        pnl = ts_service.get_trade_pnl(trade_id)
        
        assert pnl["gross_pnl"] == -1000.0  # (140 - 150) * 100

    def test_pnl_with_fees(self, ts_service, sample_order, sample_exit_order, test_db):
        """Test P&L calculation with fees."""
        # Add fees to orders
        entry_cost = OrderCost(order_id=sample_order.id, total_fee=10.0)
        exit_cost = OrderCost(order_id=sample_exit_order.id, total_fee=10.0)
        test_db.add(entry_cost)
        test_db.add(exit_cost)
        test_db.commit()
        
        trade_id = ts_service.open_trade(
            entry_order_id=sample_order.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=100,
            entry_price=150.00,
        )
        
        ts_service.close_trade(
            trade_id,
            exit_order_id=sample_exit_order.id,
            exit_qty=100,
            exit_price=160.00,
        )
        
        pnl = ts_service.get_trade_pnl(trade_id)
        
        assert pnl["total_fees"] == 20.0
        assert pnl["net_pnl"] == 980.0  # 1000 - 20


# ==================
# Position Tracking Tests
# ==================

class TestPositionTracking:
    """Test position inventory and averaging."""

    def test_new_position_on_first_buy(self, ts_service, sample_order, test_db):
        """Test position created on first trade."""
        ts_service.open_trade(
            entry_order_id=sample_order.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=100,
            entry_price=150.00,
        )
        
        pos = ts_service.get_position("AAPL")
        
        assert pos["symbol"] == "AAPL"
        assert pos["quantity"] == 100
        assert pos["avg_entry_price"] == 150.00

    def test_position_averaging_buy(self, ts_service, test_db):
        """Test position averaging on multiple BUY."""
        # Create two orders
        order1 = Order(
            order_request_id=1,
            exchange="NYSE",
            status="FILLED",
            avg_price=150.00,
            executed_qty=100,
        )
        order2 = Order(
            order_request_id=2,
            exchange="NYSE",
            status="FILLED",
            avg_price=160.00,
            executed_qty=100,
        )
        test_db.add(order1)
        test_db.add(order2)
        test_db.commit()
        
        # First trade
        trade1 = ts_service.open_trade(
            entry_order_id=order1.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=100,
            entry_price=150.00,
        )
        
        # Second trade (average up)
        trade2 = ts_service.open_trade(
            entry_order_id=order2.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=100,
            entry_price=160.00,
        )
        
        pos = ts_service.get_position("AAPL")
        
        assert pos["quantity"] == 200
        assert pos["avg_entry_price"] == 155.0  # (100*150 + 100*160) / 200


# ==================
# Trade Query Tests
# ==================

class TestTradeQueries:
    """Test trade listing and filtering."""

    def test_list_trades_empty(self, ts_service):
        """Test listing trades when none exist."""
        trades = ts_service.list_trades()
        assert len(trades) == 0

    def test_list_trades_by_symbol(self, ts_service, sample_order, test_db):
        """Test filtering trades by symbol."""
        # Create two trades
        order1 = sample_order
        order2 = Order(
            order_request_id=3,
            exchange="NYSE",
            status="FILLED",
            avg_price=100.00,
            executed_qty=50,
        )
        test_db.add(order2)
        test_db.commit()
        
        ts_service.open_trade(
            entry_order_id=order1.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=100,
            entry_price=150.00,
        )
        
        ts_service.open_trade(
            entry_order_id=order2.id,
            symbol="MSFT",
            side="BUY",
            entry_qty=50,
            entry_price=100.00,
        )
        
        # Filter by symbol
        aapl_trades = ts_service.list_trades(symbol="AAPL")
        assert len(aapl_trades) == 1
        assert aapl_trades[0]["symbol"] == "AAPL"

    def test_list_trades_by_status(self, ts_service, sample_order, sample_exit_order, test_db):
        """Test filtering trades by status."""
        # Open trade
        trade_id = ts_service.open_trade(
            entry_order_id=sample_order.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=100,
            entry_price=150.00,
        )
        
        # Close trade
        ts_service.close_trade(
            trade_id,
            exit_order_id=sample_exit_order.id,
            exit_qty=100,
            exit_price=152.00,
        )
        
        # Filter by status
        closed = ts_service.list_trades(status="CLOSED")
        assert len(closed) == 1


# ==================
# Repository Tests
# ==================

class TestRepositoryIntegration:
    """Test repository layer and SOT integration."""

    def test_get_order_from_sot(self, test_db, sample_order):
        """Test reading order from SOT."""
        order = ts_repo.TSRepository.get_order_from_sot(test_db, sample_order.id)
        
        assert order is not None
        assert order.exchange == "NYSE"
        assert order.executed_qty == 100

    def test_create_position(self, test_db):
        """Test creating position record."""
        pos = ts_repo.TSRepository.create_or_update_position(
            test_db,
            symbol="AAPL",
            quantity=100,
            avg_entry_price=150.00,
            total_traded=100,
            total_cost=15000,
        )
        
        assert pos.symbol == "AAPL"
        assert pos.quantity == 100
        assert pos.avg_entry_price == 150.00


# ==================
# Integration Tests
# ==================

class TestFullWorkflow:
    """Test complete trade workflow."""

    def test_end_to_end_trade(self, ts_service, test_db):
        """Test complete trade from open to close."""
        # Create orders
        entry_order = Order(
            order_request_id=1,
            exchange="NYSE",
            status="FILLED",
            avg_price=150.00,
            executed_qty=100,
        )
        exit_order = Order(
            order_request_id=2,
            exchange="NYSE",
            status="FILLED",
            avg_price=155.00,
            executed_qty=100,
        )
        test_db.add(entry_order)
        test_db.add(exit_order)
        test_db.commit()
        
        # Add fees
        entry_cost = OrderCost(order_id=entry_order.id, total_fee=5.0)
        exit_cost = OrderCost(order_id=exit_order.id, total_fee=5.0)
        test_db.add(entry_cost)
        test_db.add(exit_cost)
        test_db.commit()
        
        # Open trade
        trade_id = ts_service.open_trade(
            entry_order_id=entry_order.id,
            symbol="AAPL",
            side="BUY",
            entry_qty=100,
            entry_price=150.00,
            strategy_code="test_strategy",
        )
        
        # Verify position
        pos = ts_service.get_position("AAPL")
        assert pos["quantity"] == 100
        
        # Close trade
        result = ts_service.close_trade(
            trade_id,
            exit_order_id=exit_order.id,
            exit_qty=100,
            exit_price=155.00,
        )
        
        # Verify P&L
        assert result["status"] == "CLOSED"
        assert result["pnl"]["gross_pnl"] == 500.0  # (155 - 150) * 100
        assert result["pnl"]["total_fees"] == 10.0
        assert result["pnl"]["net_pnl"] == 490.0  # 500 - 10
        
        # Verify trade details
        trade_data = ts_service.get_trade(trade_id)
        assert trade_data["symbol"] == "AAPL"
        assert trade_data["status"] == "CLOSED"
        assert trade_data["pnl"]["net_pnl"] == 490.0
