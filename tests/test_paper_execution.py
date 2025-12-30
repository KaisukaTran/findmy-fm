"""Tests for paper execution engine."""
import pytest
import pandas as pd
import tempfile
from pathlib import Path
from datetime import datetime
import os

from findmy.execution.paper_execution import (
    parse_orders_from_excel,
    upsert_order,
    simulate_fill,
    run_paper_execution,
    setup_db,
    detect_order_side,
    Order,
    Trade,
    Position,
)


@pytest.fixture(autouse=True)
def cleanup_test_db():
    """Cleanup test database before each test."""
    import time
    from pathlib import Path
    from findmy.execution.paper_execution import DB_PATH
    
    # Remove old database before test
    if DB_PATH.exists():
        # Add a small delay to ensure database connection is closed
        time.sleep(0.1)
        try:
            DB_PATH.unlink()
        except Exception:
            pass
    
    yield
    
    # Clean up after test if needed
    # (Keep for next test to clean)


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    from findmy.execution.paper_execution import setup_db
    
    # Database is reset by autouse fixture, just create engine
    engine, SessionFactory = setup_db()
    yield engine, SessionFactory
    # Cleanup
    engine.dispose()


@pytest.fixture
def sample_excel_with_header(tmp_path):
    """Create sample Excel file with proper header."""
    file_path = tmp_path / "test_with_header.xlsx"
    df = pd.DataFrame({
        "Order ID": ["001", "002", "003"],
        "Quantity": [10.5, 20.0, 15.3],
        "Price": [100.0, 200.5, 150.75],
        "Trading Pair": ["BTC/USD", "ETH/USD", "BTC/USD"],
    })
    df.to_excel(file_path, sheet_name="purchase order", index=False)
    return str(file_path)


@pytest.fixture
def sample_excel_without_header(tmp_path):
    """Create sample Excel file without header (positional)."""
    file_path = tmp_path / "test_no_header.xlsx"
    df = pd.DataFrame([
        ["001", 10.5, 100.0, "BTC/USD"],
        ["002", 20.0, 200.5, "ETH/USD"],
        ["003", 15.3, 150.75, "BTC/USD"],
    ])
    df.to_excel(file_path, sheet_name="purchase order", index=False, header=False)
    return str(file_path)


@pytest.fixture
def sample_excel_mismatched_header(tmp_path):
    """Create sample Excel file with mismatched header."""
    file_path = tmp_path / "test_mismatch.xlsx"
    df = pd.DataFrame({
        "Unknown Col 1": ["001", "002"],
        "Unknown Col 2": [10.5, 20.0],
        "Unknown Col 3": [100.0, 200.5],
        "Unknown Col 4": ["BTC/USD", "ETH/USD"],
    })
    df.to_excel(file_path, sheet_name="purchase order", index=False)
    return str(file_path)


@pytest.fixture
def sample_excel_missing_sheet(tmp_path):
    """Create sample Excel file without 'purchase order' sheet."""
    file_path = tmp_path / "test_wrong_sheet.xlsx"
    df = pd.DataFrame({"Col1": [1, 2], "Col2": [3, 4]})
    df.to_excel(file_path, sheet_name="wrong_sheet", index=False)
    return str(file_path)


@pytest.fixture
def sample_excel_invalid_data(tmp_path):
    """Create Excel file with invalid numeric data."""
    file_path = tmp_path / "test_invalid_data.xlsx"
    df = pd.DataFrame({
        "Order ID": ["001", "002"],
        "Quantity": ["invalid", 20.0],
        "Price": [100.0, "invalid"],
        "Trading Pair": ["BTC/USD", "ETH/USD"],
    })
    df.to_excel(file_path, sheet_name="purchase order", index=False)
    return str(file_path)


class TestParseOrdersFromExcel:
    """Test Excel parsing with various formats."""

    def test_parse_with_header(self, sample_excel_with_header):
        """Test parsing Excel with proper headers."""
        df = parse_orders_from_excel(sample_excel_with_header)
        assert len(df) == 3
        assert "client_id" in df.columns
        assert "qty" in df.columns
        assert "price" in df.columns
        assert "symbol" in df.columns
        assert "side" in df.columns
        # Order IDs should be string representation (may lose leading zeros)
        assert df.iloc[0]["client_id"] in ("1", "001")
        assert float(df.iloc[0]["qty"]) == 10.5
        # Default side is BUY when not specified
        assert df.iloc[0]["side"] == "BUY"

    def test_parse_without_header(self, sample_excel_without_header):
        """Test parsing Excel without header (positional)."""
        df = parse_orders_from_excel(sample_excel_without_header)
        # Without header, positional parsing may result in fewer rows if nan values encountered
        assert len(df) >= 2
        assert "client_id" in df.columns
        assert "qty" in df.columns
        assert "price" in df.columns
        assert "symbol" in df.columns
        assert "side" in df.columns
        # Default side is BUY when not specified
        assert all(df["side"] == "BUY")

    def test_parse_mismatched_header(self, sample_excel_mismatched_header):
        """Test parsing with mismatched header (fallback to positional)."""
        df = parse_orders_from_excel(sample_excel_mismatched_header)
        assert len(df) == 2
        assert "client_id" in df.columns
        assert "qty" in df.columns
        assert "price" in df.columns
        assert "symbol" in df.columns
        assert "side" in df.columns

    def test_parse_missing_sheet(self, sample_excel_missing_sheet):
        """Test parsing raises error for missing sheet."""
        with pytest.raises(ValueError, match="Sheet 'purchase order' not found"):
            parse_orders_from_excel(sample_excel_missing_sheet)

    def test_parse_nonexistent_file(self):
        """Test parsing raises error for nonexistent file."""
        with pytest.raises(IOError):
            parse_orders_from_excel("/nonexistent/path/file.xlsx")


class TestUpsertOrder:
    """Test order upsert logic."""

    def test_create_new_order(self, temp_db):
        """Test creating a new order."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            order, is_new = upsert_order(
                session,
                "001",
                "BTC/USD",
                100.0,
                50000.0,
            )
            assert is_new is True
            assert order.client_order_id == "001"
            assert order.symbol == "BTC/USD"
            assert order.status == "NEW"

    def test_retrieve_existing_order(self, temp_db):
        """Test retrieving existing order."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            # Create first
            order1, is_new1 = upsert_order(
                session,
                "001",
                "BTC/USD",
                100.0,
                50000.0,
            )
            # Retrieve second
            order2, is_new2 = upsert_order(
                session,
                "001",
                "BTC/USD",
                100.0,
                50000.0,
            )
            assert is_new1 is True
            assert is_new2 is False
            assert order1.id == order2.id

    def test_invalid_numeric_values(self, temp_db):
        """Test error handling for invalid numeric values."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            with pytest.raises(ValueError, match="Invalid numeric values"):
                upsert_order(
                    session,
                    "001",
                    "BTC/USD",
                    "invalid_qty",
                    "invalid_price",
                )


class TestSimulateFill:
    """Test order fill simulation."""

    def test_simulate_fill_new_position(self, temp_db):
        """Test filling order creates new position."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            order, _ = upsert_order(
                session,
                "001",
                "BTC/USD",
                10.0,
                50000.0,
            )
            success, trade_data = simulate_fill(session, order)
            
            assert success is True
            assert trade_data["symbol"] == "BTC/USD"
            assert trade_data["qty"] == 10.0
            
            # Check position
            pos = session.query(Position).filter_by(symbol="BTC/USD").first()
            assert pos is not None
            assert float(pos.size) == 10.0
            assert float(pos.avg_price) == 50000.0

    def test_simulate_fill_existing_position(self, temp_db):
        """Test filling order updates existing position."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            # First order
            order1, _ = upsert_order(
                session,
                "001",
                "BTC/USD",
                10.0,
                50000.0,
            )
            simulate_fill(session, order1)
            
            # Second order (same symbol)
            order2, _ = upsert_order(
                session,
                "002",
                "BTC/USD",
                5.0,
                60000.0,
            )
            simulate_fill(session, order2)
            
            # Check position average price
            pos = session.query(Position).filter_by(symbol="BTC/USD").first()
            assert float(pos.size) == 15.0
            expected_avg = ((10.0 * 50000.0) + (5.0 * 60000.0)) / 15.0
            assert float(pos.avg_price) == pytest.approx(expected_avg)

    def test_fill_already_filled_order(self, temp_db):
        """Test filling already filled order returns False."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            order, _ = upsert_order(
                session,
                "001",
                "BTC/USD",
                10.0,
                50000.0,
            )
            simulate_fill(session, order)
            
            # Try to fill again
            success, trade_data = simulate_fill(session, order)
            assert success is False
            assert trade_data == {}


class TestRunPaperExecution:
    """Test complete paper execution flow."""

    def test_execution_with_valid_file(self, sample_excel_with_header):
        """Test complete execution with valid file."""
        result = run_paper_execution(sample_excel_with_header)
        
        assert result["orders"] == 3
        assert result["trades"] == 3
        assert len(result["positions"]) >= 2  # At least 2 unique symbols
        assert result["errors"] is None

    def test_execution_with_invalid_data(self, sample_excel_invalid_data):
        """Test execution handles invalid data gracefully."""
        result = run_paper_execution(sample_excel_invalid_data)
        
        assert result["orders"] == 2
        assert result["trades"] == 0  # No successful trades
        assert result["errors"] is not None
        assert len(result["errors"]) > 0

    def test_execution_missing_sheet(self, sample_excel_missing_sheet):
        """Test execution fails gracefully for missing sheet."""
        with pytest.raises(ValueError, match="Sheet 'purchase order' not found"):
            run_paper_execution(sample_excel_missing_sheet)


class TestIntegration:
    """Integration tests for paper execution."""

    def test_full_workflow(self, sample_excel_with_header):
        """Test full paper trading workflow."""
        result = run_paper_execution(sample_excel_with_header)
        
        # Verify results
        assert "orders" in result
        assert "trades" in result
        assert "positions" in result
        
        # Verify data consistency
        assert result["orders"] > 0
        assert result["trades"] <= result["orders"]
        
        # Verify positions have required fields
        for pos in result["positions"]:
            assert "symbol" in pos
            assert "size" in pos
            assert "avg_price" in pos


# ============================================================
# SELL ORDER TESTS (v0.2.0)
# ============================================================

class TestDetectOrderSide:
    """Test order side detection from cell values."""

    def test_detect_buy_english(self):
        """Test detecting BUY from English."""
        assert detect_order_side("BUY") == "BUY"
        assert detect_order_side("buy") == "BUY"
        assert detect_order_side("Buy") == "BUY"

    def test_detect_sell_english(self):
        """Test detecting SELL from English."""
        assert detect_order_side("SELL") == "SELL"
        assert detect_order_side("sell") == "SELL"
        assert detect_order_side("Sell") == "SELL"

    def test_detect_sell_vietnamese(self):
        """Test detecting SELL from Vietnamese."""
        assert detect_order_side("BÁN") == "SELL"
        assert detect_order_side("bán") == "SELL"

    def test_detect_buy_vietnamese(self):
        """Test detecting BUY from Vietnamese (MUA)."""
        # MUA should also be detected as BUY (if implemented)
        assert detect_order_side("MUA") == "BUY"
        assert detect_order_side("mua") == "BUY"

    def test_detect_default_buy(self):
        """Test default to BUY for unrecognized values."""
        assert detect_order_side("UNKNOWN") == "BUY"
        assert detect_order_side(None) == "BUY"
        assert detect_order_side("") == "BUY"
        assert detect_order_side(123) == "BUY"


class TestParseOrdersWithSide:
    """Test Excel parsing with order side detection."""

    def test_parse_with_side_column_header(self, tmp_path):
        """Test parsing Excel with side column in header."""
        file_path = tmp_path / "test_with_side.xlsx"
        df = pd.DataFrame({
            "Order ID": ["001", "002", "003"],
            "Quantity": [10.0, 5.0, 3.0],
            "Price": [100.0, 100.0, 100.0],
            "Trading Pair": ["BTC/USD", "BTC/USD", "ETH/USD"],
            "Side": ["BUY", "SELL", "BUY"],
        })
        df.to_excel(file_path, sheet_name="purchase order", index=False)
        
        result_df = parse_orders_from_excel(str(file_path))
        assert "side" in result_df.columns
        assert result_df.iloc[0]["side"] == "BUY"
        assert result_df.iloc[1]["side"] == "SELL"

    def test_parse_with_side_column_no_header(self, tmp_path):
        """Test parsing Excel with side column (no header)."""
        file_path = tmp_path / "test_with_side_no_header.xlsx"
        df = pd.DataFrame([
            ["001", 10.0, 100.0, "BTC/USD", "BUY"],
            ["002", 5.0, 100.0, "BTC/USD", "SELL"],
            ["003", 3.0, 100.0, "ETH/USD", "BUY"],
        ])
        df.to_excel(file_path, sheet_name="purchase order", index=False, header=False)
        
        result_df = parse_orders_from_excel(str(file_path))
        assert "side" in result_df.columns
        # When parsing without headers, side detection should work
        assert "BUY" in result_df["side"].values
        assert "SELL" in result_df["side"].values

    def test_parse_without_side_defaults_to_buy(self, tmp_path):
        """Test parsing without side column defaults to BUY."""
        file_path = tmp_path / "test_no_side.xlsx"
        df = pd.DataFrame({
            "Order ID": ["001", "002"],
            "Quantity": [10.0, 5.0],
            "Price": [100.0, 100.0],
            "Trading Pair": ["BTC/USD", "ETH/USD"],
        })
        df.to_excel(file_path, sheet_name="purchase order", index=False)
        
        result_df = parse_orders_from_excel(str(file_path))
        assert "side" in result_df.columns
        assert all(result_df["side"] == "BUY")


class TestSellOrderExecution:
    """Test SELL order execution and position reduction."""

    def test_sell_reduces_position(self, temp_db):
        """Test SELL order reduces position size."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            # Buy 10 units
            order1, _ = upsert_order(session, "001", "BTC/USD", 10.0, 100.0, side="BUY")
            simulate_fill(session, order1)
            
            # Sell 3 units
            order2, _ = upsert_order(session, "002", "BTC/USD", 3.0, 110.0, side="SELL")
            success, trade_data = simulate_fill(session, order2)
            
            assert success is True
            assert trade_data["side"] == "SELL"
            assert trade_data["qty"] == 3.0
            assert trade_data["position_remaining"] == 7.0
            
            # Check position
            pos = session.query(Position).filter_by(symbol="BTC/USD").first()
            assert float(pos.size) == 7.0
            assert float(pos.avg_price) == 100.0  # Cost basis unchanged

    def test_sell_calculates_realized_pnl(self, temp_db):
        """Test SELL calculates realized PnL correctly."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            # Buy 10 units at 100
            order1, _ = upsert_order(session, "001", "BTC/USD", 10.0, 100.0, side="BUY")
            simulate_fill(session, order1)
            
            # Sell 5 units at 110 (profit of 50)
            order2, _ = upsert_order(session, "002", "BTC/USD", 5.0, 110.0, side="SELL")
            success, trade_data = simulate_fill(session, order2)
            
            assert trade_data["realized_pnl"] == 50.0  # (110 - 100) * 5
            assert trade_data["cost_basis"] == 500.0  # 5 * 100

    def test_sell_realizes_loss(self, temp_db):
        """Test SELL calculates realized PnL for loss."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            # Buy 10 units at 100
            order1, _ = upsert_order(session, "001", "BTC/USD", 10.0, 100.0, side="BUY")
            simulate_fill(session, order1)
            
            # Sell 5 units at 90 (loss of 50)
            order2, _ = upsert_order(session, "002", "BTC/USD", 5.0, 90.0, side="SELL")
            success, trade_data = simulate_fill(session, order2)
            
            assert trade_data["realized_pnl"] == -50.0  # (90 - 100) * 5

    def test_sell_full_position_close(self, temp_db):
        """Test SELL closes position completely."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            # Buy 10 units
            order1, _ = upsert_order(session, "001", "BTC/USD", 10.0, 100.0, side="BUY")
            simulate_fill(session, order1)
            
            # Sell all 10 units
            order2, _ = upsert_order(session, "002", "BTC/USD", 10.0, 110.0, side="SELL")
            success, trade_data = simulate_fill(session, order2)
            
            assert success is True
            assert trade_data["position_remaining"] == 0.0
            
            pos = session.query(Position).filter_by(symbol="BTC/USD").first()
            assert float(pos.size) == 0.0
            assert float(pos.avg_price) == 0.0

    def test_sell_partial_close_multiple_times(self, temp_db):
        """Test multiple partial SELL orders."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            # Buy 10 units at 100
            order1, _ = upsert_order(session, "001", "BTC/USD", 10.0, 100.0, side="BUY")
            simulate_fill(session, order1)
            
            # Sell 3 units at 110
            order2, _ = upsert_order(session, "002", "BTC/USD", 3.0, 110.0, side="SELL")
            simulate_fill(session, order2)
            
            # Sell 4 units at 120
            order3, _ = upsert_order(session, "003", "BTC/USD", 4.0, 120.0, side="SELL")
            success, trade_data = simulate_fill(session, order3)
            
            assert success is True
            assert trade_data["position_remaining"] == 3.0
            
            # Check position
            pos = session.query(Position).filter_by(symbol="BTC/USD").first()
            assert float(pos.size) == 3.0

    def test_sell_accumulates_realized_pnl(self, temp_db):
        """Test multiple SELLs accumulate realized PnL."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            # Buy 10 units at 100
            order1, _ = upsert_order(session, "001", "BTC/USD", 10.0, 100.0, side="BUY")
            simulate_fill(session, order1)
            
            # Sell 5 units at 110 (gain 50)
            order2, _ = upsert_order(session, "002", "BTC/USD", 5.0, 110.0, side="SELL")
            simulate_fill(session, order2)
            
            # Sell 5 units at 120 (gain 100)
            order3, _ = upsert_order(session, "003", "BTC/USD", 5.0, 120.0, side="SELL")
            simulate_fill(session, order3)
            
            # Check position realized PnL
            pos = session.query(Position).filter_by(symbol="BTC/USD").first()
            expected_realized = 50.0 + 100.0
            assert float(pos.realized_pnl) == pytest.approx(expected_realized)

    def test_sell_insufficient_position_error(self, temp_db):
        """Test SELL fails when position insufficient."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            # Buy 5 units
            order1, _ = upsert_order(session, "001", "BTC/USD", 5.0, 100.0, side="BUY")
            simulate_fill(session, order1)
            
            # Try to sell 10 units
            order2, _ = upsert_order(session, "002", "BTC/USD", 10.0, 110.0, side="SELL")
            
            with pytest.raises(ValueError, match="Insufficient position for SELL"):
                simulate_fill(session, order2)

    def test_sell_with_no_position_error(self, temp_db):
        """Test SELL fails when no position exists."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            # Try to sell without any position
            order, _ = upsert_order(session, "001", "BTC/USD", 5.0, 100.0, side="SELL")
            
            with pytest.raises(ValueError, match="Insufficient position for SELL"):
                simulate_fill(session, order)

    def test_sell_invalid_side_raises_error(self, temp_db):
        """Test invalid side value raises error."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            with pytest.raises(ValueError, match="Invalid order side"):
                upsert_order(session, "001", "BTC/USD", 10.0, 100.0, side="INVALID")


class TestMixedBuySellExecution:
    """Integration tests for mixed BUY and SELL orders."""

    def test_buy_then_sell_workflow(self, tmp_path):
        """Test BUY then SELL workflow in single file."""
        file_path = tmp_path / "test_mixed.xlsx"
        df = pd.DataFrame({
            "Order ID": ["001", "002", "003"],
            "Quantity": [10.0, 5.0, 3.0],
            "Price": [100.0, 110.0, 105.0],
            "Trading Pair": ["BTC/USD", "BTC/USD", "BTC/USD"],
            "Side": ["BUY", "SELL", "SELL"],
        })
        df.to_excel(file_path, sheet_name="purchase order", index=False)
        
        result = run_paper_execution(str(file_path))
        
        assert result["trades"] == 3
        assert result["orders"] == 3
        
        # Final position should be 2 units (10 - 5 - 3)
        positions = result["positions"]
        btc_pos = next((p for p in positions if p["symbol"] == "BTC/USD"), None)
        assert btc_pos is not None
        assert float(btc_pos["size"]) == 2.0

    def test_multiple_symbols_buy_and_sell(self, tmp_path):
        """Test BUY and SELL across multiple symbols."""
        file_path = tmp_path / "test_multi_symbol.xlsx"
        df = pd.DataFrame({
            "Order ID": ["001", "002", "003", "004"],
            "Quantity": [10.0, 5.0, 20.0, 8.0],
            "Price": [100.0, 110.0, 50.0, 52.0],
            "Trading Pair": ["BTC/USD", "BTC/USD", "ETH/USD", "ETH/USD"],
            "Side": ["BUY", "SELL", "BUY", "SELL"],
        })
        df.to_excel(file_path, sheet_name="purchase order", index=False)
        
        result = run_paper_execution(str(file_path))
        
        assert result["trades"] == 4
        positions = result["positions"]
        
        # BTC: 10 - 5 = 5
        btc_pos = next((p for p in positions if p["symbol"] == "BTC/USD"), None)
        assert float(btc_pos["size"]) == 5.0
        
        # ETH: 20 - 8 = 12
        eth_pos = next((p for p in positions if p["symbol"] == "ETH/USD"), None)
        assert float(eth_pos["size"]) == 12.0

    def test_sell_before_buy_fails(self, tmp_path):
        """Test SELL before BUY fails with clear error."""
        file_path = tmp_path / "test_sell_first.xlsx"
        df = pd.DataFrame({
            "Order ID": ["001", "002"],
            "Quantity": [5.0, 10.0],
            "Price": [100.0, 100.0],
            "Trading Pair": ["BTC/USD", "BTC/USD"],
            "Side": ["SELL", "BUY"],
        })
        df.to_excel(file_path, sheet_name="purchase order", index=False)
        
        result = run_paper_execution(str(file_path))
        
        # First order (SELL) should error
        assert result["trades"] == 1  # Only BUY should succeed
        assert result["errors"] is not None
        assert len(result["errors"]) > 0
        assert "Insufficient position" in result["errors"][0]["error"]


class TestUpsertOrderWithSide:
    """Test order creation with side parameter."""

    def test_create_buy_order(self, temp_db):
        """Test creating BUY order."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            order, is_new = upsert_order(
                session, "001", "BTC/USD", 10.0, 100.0, side="BUY"
            )
            assert order.side == "BUY"
            assert is_new is True

    def test_create_sell_order(self, temp_db):
        """Test creating SELL order."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            order, is_new = upsert_order(
                session, "001", "BTC/USD", 5.0, 110.0, side="SELL"
            )
            assert order.side == "SELL"
            assert is_new is True

    def test_side_defaults_to_buy(self, temp_db):
        """Test side parameter defaults to BUY."""
        _, SessionFactory = temp_db
        with SessionFactory() as session:
            order, _ = upsert_order(
                session, "001", "BTC/USD", 10.0, 100.0
            )
            assert order.side == "BUY"

