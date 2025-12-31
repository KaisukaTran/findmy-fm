"""Tests for pending order approval workflow."""

import pytest
import sys
from pathlib import Path

# Add paths
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fastapi.testclient import TestClient
from findmy.api.main import app
from services.sot.pending_orders_service import (
    queue_order, get_pending_orders, approve_order, reject_order, count_pending
)


client = TestClient(app)


class TestPendingOrdersService:
    """Test pending orders service functions."""
    
    def test_queue_order(self):
        """Test queuing an order."""
        order = queue_order("BTC", "BUY", 0.5, 45000, "test")
        assert order.id is not None
        assert order.symbol == "BTC"
        assert order.side == "BUY"
        assert order.status.value == "pending"
    
    def test_get_pending_orders(self):
        """Test retrieving pending orders."""
        # Queue some orders
        queue_order("BTC", "BUY", 0.5, 45000, "test")
        queue_order("ETH", "SELL", 2.0, 2500, "test")
        
        pending = get_pending_orders(status="pending")
        assert len(pending) >= 2
    
    def test_approve_order(self):
        """Test approving an order."""
        order = queue_order("BTC", "BUY", 0.5, 45000, "test")
        order_id = order.id
        
        approved = approve_order(order_id)
        assert approved.status.value == "approved"
        assert approved.reviewed_by == "user"
    
    def test_reject_order(self):
        """Test rejecting an order."""
        order = queue_order("ETH", "SELL", 2.0, 2500, "test")
        order_id = order.id
        
        rejected = reject_order(order_id, note="Test rejection")
        assert rejected.status.value == "rejected"
        assert rejected.note == "Test rejection"
    
    def test_count_pending(self):
        """Test counting pending orders."""
        initial_count = count_pending()
        queue_order("BTC", "BUY", 0.5, 45000, "test")
        new_count = count_pending()
        assert new_count > initial_count


class TestPendingOrdersAPI:
    """Test pending orders API endpoints."""
    
    def test_list_pending_orders(self):
        """Test GET /api/pending."""
        # Queue an order
        order = queue_order("BTC", "BUY", 0.5, 45000, "test")
        
        response = client.get("/api/pending")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0
        
        # Find our queued order
        pending_ids = [o["id"] for o in data]
        assert order.id in pending_ids
    
    def test_list_pending_by_status(self):
        """Test filtering pending orders by status."""
        # Queue and approve an order
        order1 = queue_order("BTC", "BUY", 0.5, 45000, "test")
        approve_order(order1.id)
        
        # Queue but don't approve
        queue_order("ETH", "SELL", 2.0, 2500, "test")
        
        # Get only pending
        response = client.get("/api/pending?status=pending")
        assert response.status_code == 200
        pending_data = response.json()
        
        # Should not include approved order
        pending_ids = [o["id"] for o in pending_data]
        assert order1.id not in pending_ids
    
    def test_approve_order_via_api(self):
        """Test POST /api/pending/approve/{id}."""
        order = queue_order("BTC", "BUY", 0.5, 45000, "test")
        
        response = client.post(f"/api/pending/approve/{order.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "approved"
        assert data["order"]["id"] == order.id
    
    def test_reject_order_via_api(self):
        """Test POST /api/pending/reject/{id}."""
        order = queue_order("ETH", "SELL", 2.0, 2500, "test")
        
        response = client.post(
            f"/api/pending/reject/{order.id}",
            params={"note": "High slippage risk"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "rejected"
        assert data["order"]["id"] == order.id
        assert "High slippage" in data["order"]["note"]
    
    def test_approve_nonexistent_order(self):
        """Test approving non-existent order fails."""
        response = client.post("/api/pending/approve/99999")
        assert response.status_code == 400
    
    def test_double_approval_fails(self):
        """Test approving already-approved order fails."""
        order = queue_order("BTC", "BUY", 0.5, 45000, "test")
        
        # Approve once
        response1 = client.post(f"/api/pending/approve/{order.id}")
        assert response1.status_code == 200
        
        # Try to approve again
        response2 = client.post(f"/api/pending/approve/{order.id}")
        assert response2.status_code == 400


class TestPaperExecutionQueues:
    """Test that Excel upload queues orders instead of executing."""
    
    def test_excel_upload_returns_queued_orders(self, tmp_path):
        """Test that Excel upload queues orders for approval."""
        # Create a test Excel file
        import pandas as pd
        
        df = pd.DataFrame({
            "symbol": ["BTC", "ETH"],
            "qty": [0.5, 2.0],
            "price": [45000, 2500],
            "side": ["BUY", "SELL"],
            "client_id": ["order_1", "order_2"],
        })
        
        excel_file = tmp_path / "test_orders.xlsx"
        df.to_excel(excel_file, sheet_name="purchase order", index=False)
        
        # Upload file
        with open(excel_file, "rb") as f:
            response = client.post(
                "/paper-execution",
                files={"file": (excel_file.name, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            )
        
        assert response.status_code == 200
        data = response.json()
        
        # Should return queued orders, not executed trades
        assert "orders_queued" in data["result"]
        assert "pending_order_ids" in data["result"]
        assert data["result"]["orders_queued"] == 2
        assert len(data["result"]["pending_order_ids"]) == 2
        
        # Verify orders are in pending queue
        pending = client.get("/api/pending").json()
        pending_ids = [o["id"] for o in pending]
        for queued_id in data["result"]["pending_order_ids"]:
            assert queued_id in pending_ids


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
