"""Tests for FastAPI endpoints."""
import pytest
import pandas as pd
from pathlib import Path
import tempfile
from fastapi.testclient import TestClient

from findmy.api.main import app, UPLOAD_DIR


client = TestClient(app)


@pytest.fixture
def sample_excel_file(tmp_path):
    """Create a valid sample Excel file."""
    file_path = tmp_path / "test_orders.xlsx"
    df = pd.DataFrame({
        "Order ID": ["001", "002"],
        "Quantity": [10.5, 20.0],
        "Price": [100.0, 200.5],
        "Trading Pair": ["BTC/USD", "ETH/USD"],
    })
    df.to_excel(file_path, sheet_name="purchase order", index=False)
    return file_path


@pytest.fixture
def large_excel_file(tmp_path):
    """Create a large Excel file (>10MB)."""
    file_path = tmp_path / "large_file.xlsx"
    # Create a large dataframe that exceeds 10MB
    data = {
        "Order ID": list(range(1000000)),
        "Quantity": [1.0] * 1000000,
        "Price": [100.0] * 1000000,
        "Trading Pair": ["BTC/USD"] * 1000000,
    }
    df = pd.DataFrame(data)
    df.to_excel(file_path, sheet_name="purchase order", index=False)
    return file_path


class TestHealthCheck:
    """Test health check endpoint."""

    def test_health_check(self):
        """Test GET / returns health status."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "FINDMY FM API"


class TestPaperExecution:
    """Test paper execution endpoint."""

    def test_paper_execution_success(self, sample_excel_file):
        """Test successful paper execution."""
        with open(sample_excel_file, "rb") as f:
            files = {"file": ("test.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            response = client.post("/paper-execution", files=files)
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "result" in data
        assert "orders" in data["result"]
        assert "trades" in data["result"]
        assert "positions" in data["result"]

    def test_paper_execution_invalid_mime_type(self, tmp_path):
        """Test rejection of invalid MIME type."""
        # Create a text file
        file_path = tmp_path / "test.txt"
        file_path.write_text("This is not an Excel file")
        
        with open(file_path, "rb") as f:
            files = {"file": ("test.txt", f, "text/plain")}
            response = client.post("/paper-execution", files=files)
        
        assert response.status_code == 400
        assert "Invalid file type" in response.json()["detail"]

    def test_paper_execution_invalid_extension(self, tmp_path):
        """Test rejection of invalid file extension."""
        file_path = tmp_path / "test.pdf"
        file_path.write_text("Not a real PDF")
        
        with open(file_path, "rb") as f:
            files = {"file": ("test.pdf", f, "application/pdf")}
            response = client.post("/paper-execution", files=files)
        
        assert response.status_code == 400
        assert "Only Excel files" in response.json()["detail"]

    def test_paper_execution_file_too_large(self, large_excel_file):
        """Test rejection of oversized file."""
        with open(large_excel_file, "rb") as f:
            files = {"file": ("large.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            response = client.post("/paper-execution", files=files)
        
        assert response.status_code == 400
        assert "too large" in response.json()["detail"].lower()

    def test_paper_execution_no_file(self):
        """Test error when no file is provided."""
        response = client.post("/paper-execution")
        assert response.status_code == 422  # Unprocessable Entity

    def test_paper_execution_malformed_excel(self, tmp_path):
        """Test error handling for malformed Excel file."""
        file_path = tmp_path / "malformed.xlsx"
        file_path.write_bytes(b"This is not a valid Excel file")
        
        with open(file_path, "rb") as f:
            files = {"file": ("malformed.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            response = client.post("/paper-execution", files=files)
        
        assert response.status_code == 400
        assert "Invalid Excel file" in response.json()["detail"]


class TestErrorHandling:
    """Test error handling and validation."""

    def test_missing_sheet_in_excel(self, tmp_path):
        """Test error when 'purchase order' sheet is missing."""
        file_path = tmp_path / "wrong_sheet.xlsx"
        df = pd.DataFrame({"Col1": [1, 2], "Col2": [3, 4]})
        df.to_excel(file_path, sheet_name="wrong_sheet", index=False)
        
        with open(file_path, "rb") as f:
            files = {"file": ("wrong_sheet.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            response = client.post("/paper-execution", files=files)
        
        assert response.status_code == 400
        assert "Invalid Excel file" in response.json()["detail"]

    def test_file_cleanup_on_error(self, tmp_path):
        """Test that uploaded files are cleaned up after processing."""
        file_path = tmp_path / "test.xlsx"
        df = pd.DataFrame({
            "Order ID": ["001"],
            "Quantity": [10.0],
            "Price": [100.0],
            "Trading Pair": ["BTC/USD"],
        })
        df.to_excel(file_path, sheet_name="purchase order", index=False)
        
        with open(file_path, "rb") as f:
            files = {"file": ("test.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            response = client.post("/paper-execution", files=files)
        
        assert response.status_code == 200
        
        # Check that temporary files are cleaned up
        temp_files = list(UPLOAD_DIR.glob("*"))
        # Should be empty or minimal
        assert len(temp_files) == 0
