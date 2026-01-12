"""
Tests for Binance Precision Handling.

Verifies:
- Quantity rounding to stepSize
- Price rounding to tickSize
- Edge cases with minQty/maxQty
- Lot size filter compliance
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus


class TestBinancePrecision:
    """Test Binance exchange precision handling."""
    
    def test_quantity_rounded_to_step_size(self):
        """Test wave quantity is rounded to exchange stepSize."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        # Set specific step size
        session._step_size = 0.00001
        
        wave = session.generate_wave(0)
        
        # Quantity should be multiple of step_size
        remainder = wave.quantity % session._step_size
        assert remainder < 1e-10 or abs(remainder - session._step_size) < 1e-10
    
    def test_quantity_respects_min_qty(self):
        """Test wave quantity >= minQty."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1.0,  # Very small fund
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session._min_qty = 0.00001
        
        wave = session.generate_wave(0)
        
        # Should be at least min_qty
        assert wave.quantity >= session._min_qty
    
    def test_price_precision_btc_like(self):
        """Test price precision for BTC-like high prices."""
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
        
        # Price precision should be 2 for BTC
        assert session._price_precision == 2
        
        wave = session.generate_wave(1)
        
        # Price should have at most 2 decimal places
        price_str = f"{wave.target_price:.10f}"
        # Remove trailing zeros and check decimals
        price_decimals = len(price_str.split('.')[1].rstrip('0'))
        assert price_decimals <= 2
    
    def test_price_precision_eth_like(self):
        """Test price precision for ETH-like medium prices."""
        session = PyramidSession(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        # Price precision should be 4 for ETH-like
        assert session._price_precision == 4
    
    def test_price_precision_small_altcoin(self):
        """Test price precision for small altcoins."""
        session = PyramidSession(
            symbol="SHIB",
            entry_price=0.00001,
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=100.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        # Price precision should be 6 for small prices
        assert session._price_precision == 6
    
    def test_step_size_rounding(self):
        """Test specific step size rounding cases."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        # Test various step sizes
        for step_size in [0.001, 0.0001, 0.00001, 0.000001]:
            session._step_size = step_size
            session._min_qty = step_size
            
            wave = session.generate_wave(3)
            
            # Check it's a multiple of step_size
            ratio = wave.quantity / step_size
            assert abs(ratio - round(ratio)) < 1e-9, f"Not multiple of {step_size}"
    
    def test_very_small_quantities(self):
        """Test handling of very small quantities."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=100000.0,  # Very high price
            distance_pct=1.0,
            max_waves=5,
            isolated_fund=10.0,  # Small fund
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session._step_size = 0.00001
        session._min_qty = 0.00001
        
        wave = session.generate_wave(0)
        
        # Should be valid positive quantity
        assert wave.quantity > 0
        assert wave.quantity >= session._min_qty
    
    def test_large_wave_number_precision(self):
        """Test precision maintained at high wave numbers."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=1.0,
            max_waves=50,
            isolated_fund=100000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session._step_size = 0.00001
        session._min_qty = 0.00001
        
        # Check wave 49 (large)
        wave = session.generate_wave(49)
        
        # Quantity should still be valid
        assert wave.quantity > 0
        assert wave.target_price > 0
        
        # Check rounding
        remainder = wave.quantity % session._step_size
        assert remainder < 1e-10 or abs(remainder - session._step_size) < 1e-10
    
    def test_price_never_negative(self):
        """Test price never goes negative even with many waves."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=100.0,
            distance_pct=5.0,  # 5% per wave
            max_waves=30,  # Many waves
            isolated_fund=10000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        for i in range(session.max_waves):
            wave = session.generate_wave(i)
            assert wave.target_price > 0, f"Wave {i} has non-positive price"
    
    def test_exchange_info_fallback(self):
        """Test fallback when exchange info unavailable."""
        with patch('src.findmy.kss.pyramid.get_exchange_info', side_effect=Exception("API error")):
            session = PyramidSession(
                symbol="UNKNOWN",
                entry_price=100.0,
                distance_pct=2.0,
                max_waves=5,
                isolated_fund=500.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
        
        # Should use defaults
        assert session._min_qty == 0.00001
        assert session._step_size == 0.00001
        
        # Should still generate valid waves
        wave = session.generate_wave(0)
        assert wave.quantity > 0
        assert wave.target_price > 0


class TestBinanceEdgeCases:
    """Test edge cases for Binance precision."""
    
    def test_exactly_min_qty(self):
        """Test when calculated qty equals min_qty exactly."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=1,
            isolated_fund=1.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session._min_qty = 0.00001
        session._step_size = 0.00001
        
        wave = session.generate_wave(0)
        
        # Should be at least min_qty
        assert wave.quantity >= session._min_qty
    
    def test_rounding_up_vs_down(self):
        """Test rounding behavior at boundaries."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session._step_size = 0.0001
        session._min_qty = 0.0001
        
        # Generate multiple waves
        for i in range(5):
            wave = session.generate_wave(i)
            
            # Verify exact multiple
            steps = wave.quantity / session._step_size
            assert abs(steps - round(steps)) < 1e-9
    
    def test_price_at_precision_boundary(self):
        """Test price at precision boundary."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.123456789,  # More decimals than precision
            distance_pct=2.0,
            max_waves=5,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        wave = session.generate_wave(0)
        
        # Price should be rounded to precision
        assert wave.target_price == round(session.entry_price, session._price_precision)
    
    def test_dust_prevention(self):
        """Test that tiny dust amounts are handled."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        session._step_size = 0.00001
        session._min_qty = 0.00001
        
        wave = session.generate_wave(0)
        
        # Should not have dust below step_size
        assert wave.quantity == round(wave.quantity / session._step_size) * session._step_size
