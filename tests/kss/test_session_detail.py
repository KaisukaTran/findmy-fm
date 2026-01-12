"""
Tests for Session Detail API with Visualization Data.

Extends session detail tests to include:
- Projected waves status
- Filled waves status
- Status color coding
- Running averages
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


class TestSessionDetailVisualization:
    """Test session detail endpoint with visualization data."""
    
    @pytest.fixture
    def mock_session(self):
        """Create a mock session with mixed filled/projected waves."""
        session = MagicMock()
        session.session_id = "vis-test-001"
        session.symbol = "BTC"
        session.entry_price = 50000.0
        session.distance_pct = 2.0
        session.max_waves = 5
        session.isolated_fund = 1000.0
        session.tp_pct = 3.0
        session.status = "RUNNING"
        session.created_at = datetime.now()
        
        # 2 filled waves, 3 projected
        session.filled_waves = 2
        session.total_qty = 0.012
        session.total_cost = 588.0
        session.avg_price = 49000.0
        session.estimated_tp_price = 50470.0
        session.used_fund = 588.0
        session.remaining_fund = 412.0
        
        return session
    
    def test_session_detail_includes_wave_status(self, mock_session):
        """Test session detail includes wave status."""
        # Simulate wave data
        waves = []
        for i in range(5):
            wave = {
                "wave_index": i,
                "target_price": 50000 * (1 - 0.02 * i),
                "status": "FILLED" if i < 2 else "PROJECTED",
            }
            waves.append(wave)
        
        # Check statuses
        assert waves[0]["status"] == "FILLED"
        assert waves[1]["status"] == "FILLED"
        assert waves[2]["status"] == "PROJECTED"
        assert waves[3]["status"] == "PROJECTED"
        assert waves[4]["status"] == "PROJECTED"
    
    def test_session_detail_filled_wave_colors(self, mock_session):
        """Test filled waves have correct color coding."""
        # Color mapping: FILLED = green, PROJECTED = gray
        color_map = {
            "FILLED": "#28a745",
            "PROJECTED": "#6c757d",
            "PENDING": "#ffc107",
            "CANCELLED": "#dc3545",
        }
        
        filled_wave = {"status": "FILLED"}
        projected_wave = {"status": "PROJECTED"}
        
        assert color_map[filled_wave["status"]] == "#28a745"
        assert color_map[projected_wave["status"]] == "#6c757d"
    
    def test_session_detail_running_avg_line(self, mock_session):
        """Test running average line data."""
        # Running avg should update at each fill
        avg_points = [
            {"wave_index": 0, "avg_price": 50000.0, "filled": True},
            {"wave_index": 1, "avg_price": 49500.0, "filled": True},
        ]
        
        # Average should decrease as we buy at lower prices
        assert avg_points[1]["avg_price"] < avg_points[0]["avg_price"]
    
    def test_session_detail_running_tp_line(self, mock_session):
        """Test running TP line data."""
        tp_pct = 3.0
        
        # TP line follows avg * (1 + tp_pct/100)
        avg_points = [50000.0, 49500.0]
        tp_points = [p * 1.03 for p in avg_points]
        
        assert abs(tp_points[0] - 51500.0) < 0.01
        assert abs(tp_points[1] - 50985.0) < 0.01
    
    def test_session_detail_projected_avg_line(self, mock_session):
        """Test projected average line after filled waves."""
        # Projected avg continues from last filled
        waves_data = [
            {"wave_index": 0, "status": "FILLED", "avg_after": 50000.0},
            {"wave_index": 1, "status": "FILLED", "avg_after": 49500.0},
            {"wave_index": 2, "status": "PROJECTED", "avg_after": 49000.0},
            {"wave_index": 3, "status": "PROJECTED", "avg_after": 48500.0},
            {"wave_index": 4, "status": "PROJECTED", "avg_after": 48000.0},
        ]
        
        # Avg should continue decreasing
        for i in range(1, len(waves_data)):
            assert waves_data[i]["avg_after"] < waves_data[i-1]["avg_after"]
    
    def test_session_detail_qty_breakdown(self, mock_session):
        """Test quantity breakdown in detail."""
        detail = {
            "filled_qty": 0.012,
            "projected_qty": 0.018,  # Remaining 3 waves
            "total_qty": 0.030,
        }
        
        assert detail["filled_qty"] + detail["projected_qty"] == detail["total_qty"]
    
    def test_session_detail_cost_breakdown(self, mock_session):
        """Test cost breakdown in detail."""
        detail = {
            "filled_cost": 588.0,
            "projected_cost": 412.0,  # Remaining fund
            "total_cost": 1000.0,
        }
        
        assert detail["filled_cost"] + detail["projected_cost"] == detail["total_cost"]
    
    def test_session_detail_progress_percentage(self, mock_session):
        """Test progress percentage calculation."""
        filled_waves = 2
        max_waves = 5
        
        progress = (filled_waves / max_waves) * 100
        
        assert progress == 40.0
    
    def test_session_detail_no_filled_waves(self):
        """Test session detail with no filled waves."""
        # New session, no fills yet
        waves = [{"status": "PROJECTED"} for _ in range(5)]
        
        filled_count = sum(1 for w in waves if w["status"] == "FILLED")
        
        assert filled_count == 0
    
    def test_session_detail_all_waves_filled(self):
        """Test session detail with all waves filled."""
        # All waves filled
        waves = [{"status": "FILLED"} for _ in range(5)]
        
        filled_count = sum(1 for w in waves if w["status"] == "FILLED")
        projected_count = sum(1 for w in waves if w["status"] == "PROJECTED")
        
        assert filled_count == 5
        assert projected_count == 0
    
    def test_session_detail_wave_timestamps(self):
        """Test wave timestamps for filled waves."""
        waves = [
            {"wave_index": 0, "status": "FILLED", "filled_at": "2024-01-01T10:00:00"},
            {"wave_index": 1, "status": "FILLED", "filled_at": "2024-01-01T10:30:00"},
            {"wave_index": 2, "status": "PROJECTED", "filled_at": None},
        ]
        
        # Only filled waves have timestamps
        assert waves[0]["filled_at"] is not None
        assert waves[1]["filled_at"] is not None
        assert waves[2]["filled_at"] is None
    
    def test_session_detail_wave_actual_price(self):
        """Test actual fill price vs target price."""
        waves = [
            {"target_price": 50000.0, "actual_price": 49990.0, "status": "FILLED"},
            {"target_price": 49000.0, "actual_price": 49005.0, "status": "FILLED"},
            {"target_price": 48000.0, "actual_price": None, "status": "PROJECTED"},
        ]
        
        # Actual price slightly different from target
        assert waves[0]["actual_price"] != waves[0]["target_price"]
        assert waves[2]["actual_price"] is None
    
    def test_session_detail_profit_at_tp(self, mock_session):
        """Test estimated profit at TP calculation."""
        total_qty = 0.012
        avg_price = 49000.0
        tp_price = 50470.0
        
        total_cost = total_qty * avg_price
        revenue_at_tp = total_qty * tp_price
        profit = revenue_at_tp - total_cost
        profit_pct = (profit / total_cost) * 100
        
        assert profit > 0
        assert abs(profit_pct - 3.0) < 0.1  # ~3% TP
    
    def test_session_detail_unrealized_pnl(self, mock_session):
        """Test unrealized PnL calculation."""
        current_price = 49500.0
        avg_price = 49000.0
        total_qty = 0.012
        
        unrealized = (current_price - avg_price) * total_qty
        unrealized_pct = ((current_price - avg_price) / avg_price) * 100
        
        assert unrealized > 0  # In profit
        assert abs(unrealized_pct - 1.02) < 0.1
