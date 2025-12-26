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
    Order,
    Trade,
    Position,
)


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    # Use in-memory database for tests
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DB_PATH"] = str(Path(tmpdir) / "test.db")
        engine, SessionFactory = setup_db()
        yield engine, SessionFactory
        # Cleanup
        engine.dispose()


@pytest.fixture
def sample_excel_with_header(tmp_path):
    """Create sample Excel file with proper header."""
    file_path = tmp_path / "test_with_header.xlsx"
    df = pd.DataFrame({
        "Số Thứ Tự Lệnh": ["001", "002", "003"],
        "Khối Lượng Mua": [10.5, 20.0, 15.3],
        "Giá Đặt Lệnh": [100.0, 200.5, 150.75],
        "Cặp Tiền Ảo Giao Dịch": ["BTC/USD", "ETH/USD", "BTC/USD"],
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
        "Số Thứ Tự Lệnh": ["001", "002"],
        "Khối Lượng Mua": ["invalid", 20.0],
        "Giá Đặt Lệnh": [100.0, "invalid"],
        "Cặp Tiền Ảo Giao Dịch": ["BTC/USD", "ETH/USD"],
    })
    df.to_excel(file_path, sheet_name="purchase order", index=False)
    return str(file_path)


class TestParseOrdersFromExcel:
    """Test Excel parsing with various formats."""

    def test_parse_with_header(self, sample_excel_with_header):
        """Test parsing Excel with proper Vietnamese header."""
        df = parse_orders_from_excel(sample_excel_with_header)
        assert len(df) == 3
        assert list(df.columns) == ["client_id", "qty", "price", "symbol"]
        assert df.iloc[0]["client_id"] == "001"
        assert float(df.iloc[0]["qty"]) == 10.5

    def test_parse_without_header(self, sample_excel_without_header):
        """Test parsing Excel without header (positional)."""
        df = parse_orders_from_excel(sample_excel_without_header)
        assert len(df) == 3
        assert list(df.columns) == ["client_id", "qty", "price", "symbol"]

    def test_parse_mismatched_header(self, sample_excel_mismatched_header):
        """Test parsing with mismatched header (fallback to positional)."""
        df = parse_orders_from_excel(sample_excel_mismatched_header)
        assert len(df) == 2
        assert list(df.columns) == ["client_id", "qty", "price", "symbol"]

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
