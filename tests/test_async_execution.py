"""Tests for async/latency simulation features in paper execution engine."""
import pytest
import asyncio
from datetime import datetime
from time import time, sleep

from findmy.execution.paper_execution import (
    setup_db,
    upsert_order,
    submit_order_async,
    process_pending_orders,
    get_pending_orders,
    simulate_fill,
    DB_PATH,
    Order,
)


@pytest.fixture(autouse=True)
def cleanup_test_db_async():
    """Cleanup test database before each async test."""
    import time
    from pathlib import Path

    # Remove old database before test
    if DB_PATH.exists():
        time.sleep(0.1)
        try:
            DB_PATH.unlink()
        except Exception:
            pass

    yield

    # Clean up after test if needed
    if DB_PATH.exists():
        time.sleep(0.1)
        try:
            DB_PATH.unlink()
        except Exception:
            pass


@pytest.fixture
def temp_db_async():
    """Create temporary database for async testing."""
    from findmy.execution.paper_execution import setup_db

    engine, SessionFactory = setup_db()
    yield engine, SessionFactory


class TestAsyncOrderSubmission:
    """Tests for async order submission and latency simulation."""

    def test_submit_order_async_with_latency(self, temp_db_async):
        """Test submitting an order asynchronously with latency."""
        _, SessionFactory = temp_db_async
        with SessionFactory() as session:
            # Create order
            order, _ = upsert_order(session, "001", "BTC/USD", 10.0, 50000.0, side="BUY")

            # Submit asynchronously with latency
            result = asyncio.run(submit_order_async(session, order, latency_ms=100))

            assert result["status"] == "PENDING"
            assert result["order_id"] == order.id
            assert result["latency_ms"] == 100
            assert order.status == "PENDING"
            assert order.submitted_at is not None

    def test_submit_order_async_no_latency(self, temp_db_async):
        """Test submitting an order without latency (immediate)."""
        _, SessionFactory = temp_db_async
        with SessionFactory() as session:
            order, _ = upsert_order(session, "001", "BTC/USD", 10.0, 50000.0, side="BUY")

            result = asyncio.run(submit_order_async(session, order, latency_ms=0))

            assert result["status"] == "PENDING"
            assert result["latency_ms"] == 0

    def test_get_pending_orders(self, temp_db_async):
        """Test retrieving pending orders with execution progress."""
        _, SessionFactory = temp_db_async
        with SessionFactory() as session:
            # Create and submit orders
            order1, _ = upsert_order(session, "001", "BTC/USD", 10.0, 50000.0, side="BUY")
            asyncio.run(submit_order_async(session, order1, latency_ms=1000))

            order2, _ = upsert_order(session, "002", "ETH/USD", 50.0, 3000.0, side="BUY")
            asyncio.run(submit_order_async(session, order2, latency_ms=500))

            # Get pending orders
            pending = get_pending_orders(session)

            assert len(pending) == 2
            assert all(p["status"] == "PENDING" for p in pending)
            assert pending[0]["symbol"] == "BTC/USD"
            assert pending[1]["symbol"] == "ETH/USD"
            # Progress should be very low (< 5%)
            assert pending[0]["progress_pct"] < 5
            assert pending[1]["progress_pct"] < 5

    def test_process_pending_orders_immediate(self, temp_db_async):
        """Test processing orders with zero latency (should execute immediately)."""
        _, SessionFactory = temp_db_async
        with SessionFactory() as session:
            # Create order with zero latency
            order, _ = upsert_order(session, "001", "BTC/USD", 10.0, 50000.0, side="BUY")
            asyncio.run(submit_order_async(session, order, latency_ms=0))

            # Process immediately
            executed = asyncio.run(process_pending_orders(session))

            assert len(executed) == 1
            assert executed[0]["symbol"] == "BTC/USD"
            assert executed[0]["side"] == "BUY"
            assert executed[0]["execution_latency_ms"] == 0

            # Order should be filled
            order_updated = session.query(Order).filter_by(id=order.id).one()
            assert order_updated.status == "FILLED"
            assert order_updated.executed_at is not None

    def test_process_pending_orders_with_delay(self, temp_db_async):
        """Test processing orders with latency (should wait before executing)."""
        _, SessionFactory = temp_db_async
        with SessionFactory() as session:
            # Create order with 100ms latency
            order, _ = upsert_order(session, "001", "BTC/USD", 10.0, 50000.0, side="BUY")
            asyncio.run(submit_order_async(session, order, latency_ms=100))

            # Process immediately (should not execute)
            executed = asyncio.run(process_pending_orders(session))
            assert len(executed) == 0

            # Order should still be pending
            order_updated = session.query(Order).filter_by(id=order.id).one()
            assert order_updated.status == "PENDING"

            # Wait and process again
            sleep(0.15)  # 150ms (exceeds 100ms latency)
            executed = asyncio.run(process_pending_orders(session))

            assert len(executed) == 1
            # Order should now be filled
            order_updated = session.query(Order).filter_by(id=order.id).one()
            assert order_updated.status == "FILLED"

    def test_async_order_processor_background_task(self, temp_db_async):
        """Test async order processor background task."""
        _, SessionFactory = temp_db_async
        with SessionFactory() as session:
            # Create orders with various latencies
            order1, _ = upsert_order(session, "001", "BTC/USD", 10.0, 50000.0, side="BUY")
            asyncio.run(submit_order_async(session, order1, latency_ms=50))

            order2, _ = upsert_order(session, "002", "ETH/USD", 50.0, 3000.0, side="BUY")
            asyncio.run(submit_order_async(session, order2, latency_ms=100))

            # Run background processor with timeout
            from findmy.execution.paper_execution import async_order_processor

            result = asyncio.run(async_order_processor(session, check_interval_ms=10, timeout_sec=1))

            assert result["processed_orders"] == 2
            assert result["elapsed_sec"] < 1.0

            # Both orders should be filled
            order1_updated = session.query(Order).filter_by(id=order1.id).one()
            order2_updated = session.query(Order).filter_by(id=order2.id).one()
            assert order1_updated.status == "FILLED"
            assert order2_updated.status == "FILLED"

    def test_async_sell_order_execution(self, temp_db_async):
        """Test asynchronous SELL order execution with PnL calculation."""
        _, SessionFactory = temp_db_async
        with SessionFactory() as session:
            # Buy first
            buy_order, _ = upsert_order(session, "buy_001", "BTC/USD", 10.0, 50000.0, side="BUY")
            simulate_fill(session, buy_order)

            # Submit sell order asynchronously
            sell_order, _ = upsert_order(
                session, "sell_001", "BTC/USD", 5.0, 55000.0, side="SELL"
            )
            asyncio.run(submit_order_async(session, sell_order, latency_ms=0))

            # Process
            executed = asyncio.run(process_pending_orders(session))

            assert len(executed) == 1
            assert executed[0]["realized_pnl"] > 0  # Profit

    def test_pending_order_progress_calculation(self, temp_db_async):
        """Test progress calculation for pending orders."""
        _, SessionFactory = temp_db_async
        with SessionFactory() as session:
            order, _ = upsert_order(session, "001", "BTC/USD", 10.0, 50000.0, side="BUY")
            asyncio.run(submit_order_async(session, order, latency_ms=300))

            # Check progress immediately (should be low)
            pending = get_pending_orders(session)
            assert pending[0]["progress_pct"] < 5

            # Wait a bit
            sleep(0.15)  # 150ms
            pending = get_pending_orders(session)
            # Progress should be around 50% (150/300)
            assert 40 <= pending[0]["progress_pct"] <= 60

            # Wait more
            sleep(0.20)  # Total 350ms (exceeds 300ms)
            pending = get_pending_orders(session)
            # Should be complete or close to it
            assert pending[0]["progress_pct"] >= 100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

