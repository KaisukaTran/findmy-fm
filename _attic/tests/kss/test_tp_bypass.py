"""
Tests for TP Bypass Logic.

Verifies:
- TP can trigger even after max_waves reached
- TP order bypasses wave limits
- TP is market order (not wave-based)
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus


class TestTPBypass:
    """Test TP trigger bypasses wave limits."""
    
    def test_tp_triggers_after_max_waves(self):
        """Test TP can trigger after all waves sent."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=3,  # Small number for testing
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 1
        session.start()
        
        # Fill all waves
        for i in range(session.max_waves):
            if i < len(session.waves):
                wave = session.waves[i]
            else:
                wave = session.generate_wave(i)
                session.waves.append(wave)
            
            with patch.object(session, '_check_timeout', return_value=False):
                with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 48000.0}):
                    result = session.on_fill(i, wave.quantity, wave.target_price)
        
        # Now at max_waves, check TP at high price
        assert session.current_wave == session.max_waves - 1 or len(session.waves) == session.max_waves
        
        # TP should still trigger
        tp_result = session.check_tp(current_market_price=55000.0)
        
        assert tp_result is not None
        assert tp_result["action"] == "tp_triggered"
    
    def test_tp_order_is_market_not_limit(self):
        """Test TP order is market order, not limit."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 1
        session.status = PyramidSessionStatus.ACTIVE
        session.total_filled_qty = 0.01
        session.avg_price = 49000.0
        session.total_cost = 490.0
        
        # Trigger TP
        tp_result = session.check_tp(current_market_price=52000.0)
        
        assert tp_result is not None
        order = tp_result.get("order")
        assert order is not None
        assert order["order_type"] == "MARKET"
        assert order["side"] == "SELL"
    
    def test_tp_sells_full_position(self):
        """Test TP sells entire filled position."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 1
        session.status = PyramidSessionStatus.ACTIVE
        session.total_filled_qty = 0.05
        session.avg_price = 48000.0
        session.total_cost = 2400.0
        
        tp_result = session.check_tp(current_market_price=52000.0)
        
        order = tp_result.get("order")
        assert order["quantity"] == session.total_filled_qty
    
    def test_tp_has_correct_source_ref(self):
        """Test TP order has correct source reference."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 42
        session.status = PyramidSessionStatus.ACTIVE
        session.total_filled_qty = 0.01
        session.avg_price = 49000.0
        
        tp_result = session.check_tp(current_market_price=55000.0)
        
        order = tp_result.get("order")
        assert "pyramid:42:tp" in order["source_ref"]
    
    def test_tp_no_trigger_without_position(self):
        """Test TP doesn't trigger without filled position."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 1
        session.status = PyramidSessionStatus.ACTIVE
        session.total_filled_qty = 0  # No position
        
        tp_result = session.check_tp(current_market_price=100000.0)
        
        # Should not trigger without position
        assert tp_result is None
    
    def test_tp_no_trigger_zero_price(self):
        """Test TP doesn't trigger with zero market price."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 1
        session.status = PyramidSessionStatus.ACTIVE
        session.total_filled_qty = 0.01
        session.avg_price = 49000.0
        
        tp_result = session.check_tp(current_market_price=0)
        
        assert tp_result is None
    
    def test_tp_changes_status(self):
        """Test TP changes session status to TP_TRIGGERED."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 1
        session.status = PyramidSessionStatus.ACTIVE
        session.total_filled_qty = 0.01
        session.avg_price = 49000.0
        
        assert session.status == PyramidSessionStatus.ACTIVE
        
        session.check_tp(current_market_price=55000.0)
        
        assert session.status == PyramidSessionStatus.TP_TRIGGERED
    
    def test_tp_at_exact_threshold(self):
        """Test TP at exact threshold price."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,  # 3% above avg
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 1
        session.status = PyramidSessionStatus.ACTIVE
        session.total_filled_qty = 0.01
        session.avg_price = 50000.0
        session.total_cost = 500.0
        
        # Exact TP price = 50000 * 1.03 = 51500
        tp_price = 50000.0 * 1.03
        
        # At exact threshold should trigger
        tp_result = session.check_tp(current_market_price=tp_price)
        assert tp_result is not None
        assert tp_result["action"] == "tp_triggered"
    
    def test_tp_below_threshold(self):
        """Test TP does not trigger below threshold."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 1
        session.status = PyramidSessionStatus.ACTIVE
        session.total_filled_qty = 0.01
        session.avg_price = 50000.0
        
        # Just below threshold
        tp_result = session.check_tp(current_market_price=51499.0)
        
        assert tp_result is None
        assert session.status == PyramidSessionStatus.ACTIVE


class TestTPCalculation:
    """Test TP price calculation."""
    
    def test_tp_price_calculation(self):
        """Test TP price is correctly calculated."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=5.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.avg_price = 48000.0
        
        expected_tp = 48000.0 * 1.05  # 50400
        assert abs(session.estimated_tp_price - expected_tp) < 0.01
    
    def test_tp_price_no_fills(self):
        """Test TP price uses entry when no fills."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        # No fills yet, avg_price = 0
        assert session.avg_price == 0
        
        # Should use entry price
        expected_tp = 50000.0 * 1.03
        assert abs(session.estimated_tp_price - expected_tp) < 0.01
    
    def test_tp_updates_after_fills(self):
        """Test TP price updates after each fill."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=100.0,
            distance_pct=5.0,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=10.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session.id = 1
        session.start()
        
        initial_tp = session.estimated_tp_price
        
        # Fill wave 0
        wave = session.waves[0]
        with patch.object(session, '_check_timeout', return_value=False):
            with patch('src.findmy.kss.pyramid.get_current_prices', return_value={"BTC": 90.0}):
                session.on_fill(0, wave.quantity, wave.target_price)
        
        # TP should update based on new avg
        new_tp = session.estimated_tp_price
        
        # After fill at entry price, avg = entry, so TP should be similar
        assert abs(new_tp - initial_tp) < 5.0  # Within tolerance
