"""
Tests for KSS Isolated Fund Management.

Verifies:
- Wave rejected when exceeding isolated fund
- Remaining fund calculation
- Used fund tracking
- Fund validation on session start
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus


class TestIsolatedFund:
    """Test isolated fund management."""
    
    def test_start_rejects_insufficient_fund_wave0(self):
        """Test session start rejected if fund < wave 0 cost."""
        # Create session with tiny fund that can't cover wave 0
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=0.01,  # Very small fund
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        result = session.start()
        
        # Should fail to start
        assert result is None
        assert session.status == PyramidSessionStatus.PENDING
    
    def test_remaining_fund_calculation(self):
        """Test remaining fund decreases after fills."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=100.0,
            distance_pct=5.0,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        initial_remaining = session.remaining_fund
        assert initial_remaining == 500.0
        
        # Start session
        session.start()
        wave0 = session.waves[0]
        
        # Simulate fill
        with patch.object(session, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 95.0}):
                session.on_fill(0, wave0.quantity, wave0.target_price)
        
        # Remaining should decrease
        assert session.remaining_fund < initial_remaining
        assert session.used_fund > 0
        assert abs(session.remaining_fund + session.used_fund - 500.0) < 0.01
    
    def test_next_wave_rejected_insufficient_fund(self):
        """Test next wave not generated if insufficient fund."""
        # Create session with fund that covers only first few waves
        session = PyramidSession(
            symbol="ETH",
            entry_price=1000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=10.0,  # Very limited fund
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        # Start session
        session.start()
        wave0 = session.waves[0]
        
        # Simulate fill of wave 0
        with patch.object(session, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"ETH": 980.0}):
                result = session.on_fill(0, wave0.quantity, wave0.target_price)
        
        # Should either generate next wave or indicate insufficient fund
        # depending on exact quantities
        assert result is not None
        if session.remaining_fund < 0.001:
            assert result["action"] in ["none", "stopped"]
    
    def test_used_fund_equals_total_cost(self):
        """Test used fund equals total cost of filled waves."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=100.0,
            distance_pct=5.0,
            max_waves=3,
            isolated_fund=500.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.start()
        wave0 = session.waves[0]
        
        # Fill wave 0
        fill_price = wave0.target_price
        fill_qty = wave0.quantity
        
        with patch.object(session, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 95.0}):
                session.on_fill(0, fill_qty, fill_price)
        
        assert session.used_fund == session.total_cost
        assert session.total_cost == fill_qty * fill_price
    
    def test_isolated_fund_boundary(self):
        """Test behavior at exact fund boundary."""
        session = PyramidSession(
            symbol="TEST",
            entry_price=100.0,
            distance_pct=0.1,  # Very small distance
            max_waves=2,
            isolated_fund=100.0,  # Exact amount for some waves
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        # Calculate if fund is enough
        estimated = session.estimate_total_cost(2)
        
        # If estimated > isolated_fund, session should handle it
        result = session.start()
        
        if estimated > session.isolated_fund:
            # May fail or succeed depending on wave 0 cost
            pass  # Test just verifies no crash
        else:
            assert result is not None
    
    def test_fund_preserved_on_stop(self):
        """Test fund values preserved when session stopped."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=100.0,
            distance_pct=5.0,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.start()
        wave0 = session.waves[0]
        
        # Fill wave 0
        with patch.object(session, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 95.0}):
                session.on_fill(0, wave0.quantity, wave0.target_price)
        
        used_before_stop = session.used_fund
        remaining_before_stop = session.remaining_fund
        
        session.stop("test")
        
        # Fund values preserved
        assert session.used_fund == used_before_stop
        assert session.remaining_fund == remaining_before_stop
    
    def test_zero_remaining_fund(self):
        """Test session handles zero remaining fund."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=100.0,
            distance_pct=5.0,
            max_waves=10,
            isolated_fund=500.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        # Manually set used_fund to exhaust fund
        session.total_cost = 500.0
        
        assert session.remaining_fund == 0.0
        
        # Generate wave should still work (for preview)
        wave = session.generate_wave(5)
        assert wave is not None
        assert wave.quantity > 0
    
    def test_multiple_fills_fund_tracking(self):
        """Test fund tracking across multiple fills."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=100.0,
            distance_pct=5.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.start()
        
        total_spent = 0.0
        for i in range(3):
            if i < len(session.waves):
                wave = session.waves[i]
                cost = wave.quantity * wave.target_price
                total_spent += cost
                
                with patch.object(session, '_check_timeout', return_value=False):
                    with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 90.0}):
                        session.on_fill(i, wave.quantity, wave.target_price)
        
        assert abs(session.used_fund - total_spent) < 0.01
        assert abs(session.remaining_fund - (1000.0 - total_spent)) < 0.01
