"""
Tests for advanced execution features (v0.3.0)

Tests cover:
- Partial fill support
- Fee and slippage modeling
- Enhanced reporting
"""

import pytest
from datetime import datetime
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.findmy.execution.paper_execution import (
    setup_db, Base, Order, Trade, Position
)
from src.findmy.execution.advanced import (
    calculate_partial_fill_qty,
    apply_execution_costs,
    simulate_partial_fill,
    simulate_full_fill_with_costs,
    PartialFillResult,
)
from src.findmy.execution.config import (
    ExecutionConfig, PartialFillConfig, FeeConfig, SlippageConfig
)


@pytest.fixture
def in_memory_db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    yield SessionFactory
    engine.dispose()


@pytest.fixture
def session(in_memory_db):
    """Create a session for a test."""
    session = in_memory_db()
    yield session
    session.close()


class TestPartialFillConfig:
    """Test partial fill configuration."""
    
    def test_fixed_fill_percentage(self):
        """Test fixed fill percentage."""
        config = PartialFillConfig(
            enabled=True,
            fill_type="fixed",
            fill_percentage=0.5
        )
        
        fill_qty = config.get_fill_qty(100.0)
        assert fill_qty == 50.0
    
    def test_random_fill_range(self):
        """Test random fill within range."""
        config = PartialFillConfig(
            enabled=True,
            fill_type="random",
            min_fill_pct=0.25,
            max_fill_pct=0.75
        )
        
        # Test multiple times to ensure it's within range
        for _ in range(10):
            fill_qty = config.get_fill_qty(100.0)
            assert 25.0 <= fill_qty <= 75.0
    
    def test_disabled_returns_full_qty(self):
        """Test that disabled partial fills returns full quantity."""
        config = PartialFillConfig(enabled=False)
        
        fill_qty = config.get_fill_qty(100.0)
        assert fill_qty == 100.0


class TestExecutionCosts:
    """Test fee and slippage modeling."""
    
    def test_fee_calculation_taker(self):
        """Test taker fee calculation."""
        config = FeeConfig(taker_fee_pct=0.001, enabled=True)
        
        notional = 100.0 * 50000.0  # 100 BTC at $50k
        fee = config.calculate_fee(notional, is_maker=False)
        
        assert fee == pytest.approx(notional * 0.001)
    
    def test_fee_calculation_maker(self):
        """Test maker fee calculation."""
        config = FeeConfig(maker_fee_pct=0.0005, taker_fee_pct=0.001, enabled=True)
        
        notional = 100.0 * 50000.0
        maker_fee = config.calculate_fee(notional, is_maker=True)
        
        assert maker_fee == pytest.approx(notional * 0.0005)
    
    def test_fee_disabled(self):
        """Test that disabled fees return 0."""
        config = FeeConfig(enabled=False)
        fee = config.calculate_fee(100000.0)
        
        assert fee == 0.0
    
    def test_slippage_buy_order(self):
        """Test slippage for BUY order (price increases)."""
        config = SlippageConfig(
            enabled=True,
            slippage_type="fixed",
            slippage_bps=10.0  # 10 basis points
        )
        
        original_price = 50000.0
        slipped_price, slippage_amount = config.apply_slippage(original_price, "BUY")
        
        # BUY should get worse price (higher)
        assert slipped_price > original_price
        assert slippage_amount > 0
    
    def test_slippage_sell_order(self):
        """Test slippage for SELL order (price decreases)."""
        config = SlippageConfig(
            enabled=True,
            slippage_type="fixed",
            slippage_bps=10.0
        )
        
        original_price = 50000.0
        slipped_price, slippage_amount = config.apply_slippage(original_price, "SELL")
        
        # SELL should get worse price (lower)
        assert slipped_price < original_price
        assert slippage_amount > 0
    
    def test_slippage_disabled(self):
        """Test that disabled slippage returns original price."""
        config = SlippageConfig(enabled=False)
        
        original_price = 50000.0
        slipped_price, slippage_amount = config.apply_slippage(original_price, "BUY")
        
        assert slipped_price == original_price
        assert slippage_amount == 0.0


class TestApplyExecutionCosts:
    """Test apply_execution_costs function."""
    
    def test_buy_order_with_fees_and_slippage(self):
        """Test BUY order with both fees and slippage."""
        config = ExecutionConfig(
            fees=FeeConfig(taker_fee_pct=0.001, enabled=True),
            slippage=SlippageConfig(
                enabled=True,
                slippage_type="fixed",
                slippage_bps=5.0
            )
        )
        
        qty = 1.0
        price = 50000.0
        
        effective_price, fees, slippage = apply_execution_costs(
            qty, price, "BUY", config
        )
        
        # Effective price should be higher than original (both slippage and fees)
        assert effective_price > price
        assert fees > 0
        assert slippage > 0
    
    def test_sell_order_with_costs(self):
        """Test SELL order with fees and slippage."""
        config = ExecutionConfig(
            fees=FeeConfig(taker_fee_pct=0.001, enabled=True),
            slippage=SlippageConfig(
                enabled=True,
                slippage_type="fixed",
                slippage_bps=5.0
            )
        )
        
        qty = 1.0
        price = 50000.0
        
        effective_price, fees, slippage = apply_execution_costs(
            qty, price, "SELL", config
        )
        
        # Effective price should be lower than original (both slippage and fees reduce proceeds)
        assert effective_price < price
        assert fees > 0
        assert slippage > 0


class TestPartialFillSimulation:
    """Test partial fill execution."""
    
    def test_partial_buy_fill_creates_position(self, session):
        """Test that partial BUY fill creates position."""
        order = Order(
            client_order_id="TEST001",
            symbol="BTC/USD",
            side="BUY",
            qty=Decimal("1.0"),
            price=Decimal("50000.0"),
            status="NEW"
        )
        session.add(order)
        session.commit()
        
        config = ExecutionConfig(
            partial_fill=PartialFillConfig(
                enabled=True,
                fill_type="fixed",
                fill_percentage=0.5
            ),
            fees=FeeConfig(enabled=False),
            slippage=SlippageConfig(enabled=False)
        )
        
        success, result = simulate_partial_fill(session, order, config)
        
        assert success
        assert result.filled_qty == 0.5
        assert result.remaining_qty == 0.5
        assert order.status == "PARTIAL"
        
        # Check position was created
        pos = session.query(Position).filter_by(symbol="BTC/USD").first()
        assert pos is not None
        assert float(pos.size) == 0.5
    
    def test_partial_sell_reduces_position(self, session):
        """Test that partial SELL fill reduces position."""
        # Create position first
        pos = Position(
            symbol="BTC/USD",
            size=Decimal("2.0"),
            avg_price=Decimal("45000.0"),
            realized_pnl=Decimal("0.0")
        )
        session.add(pos)
        session.commit()
        
        # Create SELL order
        order = Order(
            client_order_id="TEST002",
            symbol="BTC/USD",
            side="SELL",
            qty=Decimal("1.0"),
            price=Decimal("50000.0"),
            status="NEW"
        )
        session.add(order)
        session.commit()
        
        config = ExecutionConfig(
            partial_fill=PartialFillConfig(
                enabled=True,
                fill_type="fixed",
                fill_percentage=0.5
            ),
            fees=FeeConfig(enabled=False),
            slippage=SlippageConfig(enabled=False)
        )
        
        success, result = simulate_partial_fill(session, order, config)
        
        assert success
        assert result.filled_qty == 0.5
        assert result.remaining_qty == 0.5
        
        # Check position was reduced
        pos = session.query(Position).filter_by(symbol="BTC/USD").first()
        assert float(pos.size) == 1.5  # 2.0 - 0.5
    
    def test_oversell_prevention(self, session):
        """Test that overselling is prevented."""
        # Create small position
        pos = Position(
            symbol="BTC/USD",
            size=Decimal("0.5"),
            avg_price=Decimal("45000.0"),
            realized_pnl=Decimal("0.0")
        )
        session.add(pos)
        session.commit()
        
        # Try to sell 1.0 (more than position)
        order = Order(
            client_order_id="TEST003",
            symbol="BTC/USD",
            side="SELL",
            qty=Decimal("1.0"),
            price=Decimal("50000.0"),
            status="NEW"
        )
        session.add(order)
        session.commit()
        
        config = ExecutionConfig()
        success, result = simulate_partial_fill(session, order, config)
        
        assert not success


class TestFullFillWithCosts:
    """Test full fill simulation with multiple partial fills."""
    
    def test_full_fill_multiple_iterations(self, session):
        """Test order fills completely through multiple partial fills."""
        order = Order(
            client_order_id="TEST004",
            symbol="BTC/USD",
            side="BUY",
            qty=Decimal("10.0"),
            price=Decimal("50000.0"),
            status="NEW"
        )
        session.add(order)
        session.commit()
        
        config = ExecutionConfig(
            partial_fill=PartialFillConfig(
                enabled=True,
                fill_type="fixed",
                fill_percentage=0.3  # Fill 30% each time
            ),
            fees=FeeConfig(taker_fee_pct=0.001, enabled=True),
            slippage=SlippageConfig(enabled=False)
        )
        
        success, summary = simulate_full_fill_with_costs(session, order, config)
        
        assert success
        assert summary["total_filled"] == pytest.approx(10.0, rel=0.01)
        assert order.status == "FILLED"
        assert len(summary["trades"]) >= 3  # Multiple fills
        assert summary["total_fees"] > 0
    
    def test_summary_includes_all_metrics(self, session):
        """Test that summary includes all cost metrics."""
        order = Order(
            client_order_id="TEST005",
            symbol="ETH/USD",
            side="BUY",
            qty=Decimal("10.0"),
            price=Decimal("3000.0"),
            status="NEW"
        )
        session.add(order)
        session.commit()
        
        config = ExecutionConfig(
            partial_fill=PartialFillConfig(
                enabled=True,
                fill_type="fixed",
                fill_percentage=1.0  # Full fill at once
            ),
            fees=FeeConfig(taker_fee_pct=0.001, enabled=True),
            slippage=SlippageConfig(
                enabled=True,
                slippage_type="fixed",
                slippage_bps=5.0
            )
        )
        
        success, summary = simulate_full_fill_with_costs(session, order, config)
        
        assert success
        assert "total_fees" in summary
        assert "total_slippage" in summary
        assert "average_effective_price" in summary
        assert summary["average_effective_price"] > 3000.0  # Higher due to slippage/fees


class TestPartialFillResult:
    """Test PartialFillResult data class."""
    
    def test_to_dict_conversion(self):
        """Test conversion to dictionary."""
        result = PartialFillResult()
        result.filled_qty = 5.0
        result.remaining_qty = 5.0
        result.original_qty = 10.0
        result.total_fees = 10.0
        result.total_slippage = 5.0
        result.status = "PARTIAL"
        
        d = result.to_dict()
        
        assert d["filled_qty"] == 5.0
        assert d["remaining_qty"] == 5.0
        assert d["fill_ratio"] == 0.5
        assert d["fees"] == 10.0
        assert d["slippage"] == 5.0
        assert d["status"] == "PARTIAL"
