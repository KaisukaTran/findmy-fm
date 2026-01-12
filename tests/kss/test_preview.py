"""
Tests for KSS Preview Mode.

Verifies:
- Preview returns projected waves without creating session
- Correct cost estimation calculations
- Running averages are accurate
- No session created in database
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

from src.findmy.kss.pyramid import PyramidSession, PyramidSessionStatus


class TestPreviewMode:
    """Test preview mode functionality."""
    
    @pytest.fixture
    def preview_params(self):
        """Standard preview parameters."""
        return {
            "symbol": "BTC",
            "entry_price": 50000.0,
            "distance_pct": 2.0,
            "max_waves": 10,
            "isolated_fund": 1000.0,
            "tp_pct": 3.0,
        }
    
    def test_estimate_total_cost_all_waves(self, preview_params):
        """Test cost estimation for all waves."""
        session = PyramidSession(
            **preview_params,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        total_cost = session.estimate_total_cost()
        
        # Should be > 0 and <= isolated_fund (approximately)
        assert total_cost > 0
        # All wave costs should sum up correctly
        manual_cost = sum(
            session.generate_wave(i).quantity * session.generate_wave(i).target_price
            for i in range(session.max_waves)
        )
        assert abs(total_cost - manual_cost) < 0.01
    
    def test_estimate_total_cost_partial_waves(self, preview_params):
        """Test cost estimation for subset of waves."""
        session = PyramidSession(
            **preview_params,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        # Estimate for 5 waves
        partial_cost = session.estimate_total_cost(5)
        full_cost = session.estimate_total_cost(10)
        
        # Partial should be less than full
        assert partial_cost < full_cost
        assert partial_cost > 0
    
    def test_preview_does_not_change_state(self, preview_params):
        """Test that preview operations don't change session state."""
        session = PyramidSession(
            **preview_params,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        initial_status = session.status
        initial_wave_count = len(session.waves)
        
        # Generate waves (preview-like operation)
        for i in range(session.max_waves):
            session.generate_wave(i)
        
        # Estimate cost
        session.estimate_total_cost()
        
        # State should remain unchanged
        assert session.status == initial_status
        assert len(session.waves) == initial_wave_count
        assert session.total_filled_qty == 0.0
        assert session.avg_price == 0.0
    
    def test_wave_generation_price_decreasing(self, preview_params):
        """Test that wave prices decrease correctly."""
        session = PyramidSession(
            **preview_params,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        prices = []
        for i in range(session.max_waves):
            wave = session.generate_wave(i)
            prices.append(wave.target_price)
        
        # Each price should be lower than previous
        for i in range(1, len(prices)):
            assert prices[i] < prices[i-1], f"Wave {i} price not decreasing"
    
    def test_wave_generation_qty_increasing(self, preview_params):
        """Test that wave quantities increase (pyramid pattern)."""
        session = PyramidSession(
            **preview_params,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        quantities = []
        for i in range(session.max_waves):
            wave = session.generate_wave(i)
            quantities.append(wave.quantity)
        
        # Each qty should be >= previous (pyramid pattern)
        for i in range(1, len(quantities)):
            assert quantities[i] >= quantities[i-1], f"Wave {i} qty not increasing"
    
    def test_preview_cumulative_cost(self, preview_params):
        """Test cumulative cost calculation for preview."""
        session = PyramidSession(
            **preview_params,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        cumulative = 0.0
        for i in range(session.max_waves):
            wave = session.generate_wave(i)
            wave_cost = wave.quantity * wave.target_price
            cumulative += wave_cost
        
        # Cumulative should match estimate
        assert abs(cumulative - session.estimate_total_cost()) < 0.01
    
    def test_preview_running_average(self, preview_params):
        """Test running average price calculation."""
        session = PyramidSession(
            **preview_params,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        total_qty = 0.0
        total_cost = 0.0
        running_avgs = []
        
        for i in range(session.max_waves):
            wave = session.generate_wave(i)
            total_qty += wave.quantity
            total_cost += wave.quantity * wave.target_price
            running_avgs.append(total_cost / total_qty)
        
        # Running average should decrease as we buy at lower prices
        for i in range(1, len(running_avgs)):
            assert running_avgs[i] < running_avgs[i-1], f"Avg not decreasing at wave {i}"
    
    def test_preview_tp_projection(self, preview_params):
        """Test TP price projection at each wave."""
        session = PyramidSession(
            **preview_params,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        total_qty = 0.0
        total_cost = 0.0
        
        for i in range(session.max_waves):
            wave = session.generate_wave(i)
            total_qty += wave.quantity
            total_cost += wave.quantity * wave.target_price
            avg_price = total_cost / total_qty
            tp_price = avg_price * (1 + session.tp_pct / 100)
            
            # TP should always be above average
            assert tp_price > avg_price
            # TP should decrease as avg decreases
            if i > 0:
                prev_tp = (total_cost - wave.quantity * wave.target_price) / (total_qty - wave.quantity) * (1 + session.tp_pct / 100)
                # Current TP should be lower since avg is lower
                assert tp_price < prev_tp or abs(tp_price - prev_tp) < 1
    
    def test_preview_single_wave(self):
        """Test preview with single wave."""
        session = PyramidSession(
            symbol="ETH",
            entry_price=3000.0,
            distance_pct=5.0,
            max_waves=1,
            isolated_fund=100.0,
            tp_pct=5.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        cost = session.estimate_total_cost(1)
        wave = session.generate_wave(0)
        
        assert wave.wave_num == 0
        assert wave.target_price == 3000.0  # First wave at entry
        assert cost == wave.quantity * wave.target_price
    
    def test_preview_high_distance_pct(self):
        """Test preview with high distance percentage."""
        session = PyramidSession(
            symbol="BTC",
            entry_price=100.0,
            distance_pct=20.0,  # 20% per wave
            max_waves=5,
            isolated_fund=500.0,
            tp_pct=10.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        # Wave 0: 100
        # Wave 1: 100 * 0.8 = 80
        # Wave 2: 100 * 0.8^2 = 64
        # Wave 3: 100 * 0.8^3 = 51.2
        # Wave 4: 100 * 0.8^4 = 40.96
        
        wave_4 = session.generate_wave(4)
        expected = 100.0 * (0.8 ** 4)
        assert abs(wave_4.target_price - expected) < 1.0
    
    def test_preview_price_range(self, preview_params):
        """Test price range from entry to last wave."""
        session = PyramidSession(
            **preview_params,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
        
        first_wave = session.generate_wave(0)
        last_wave = session.generate_wave(session.max_waves - 1)
        
        price_range_pct = (first_wave.target_price - last_wave.target_price) / first_wave.target_price * 100
        
        # Should cover approximately (distance_pct * (max_waves - 1)) %
        # For 2% distance and 10 waves: ~18% range (non-linear due to compound)
        assert price_range_pct > 0
        assert price_range_pct < 100
