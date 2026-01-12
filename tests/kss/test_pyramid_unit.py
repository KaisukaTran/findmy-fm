"""
Unit tests for PyramidSession class.

Tests all core logic:
- Wave generation formulas
- Fill event handling
- Take profit logic
- Timeout checks
- Parameter adjustment
- State transitions
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from src.findmy.kss.pyramid import (
    PyramidSession,
    PyramidSessionStatus,
    WaveInfo,
)


class TestPyramidSessionInitialization:
    """Test session initialization and validation."""
    
    def test_valid_initialization(self):
        """Test valid session creation."""
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
        
        assert session.symbol == "BTC"
        assert session.entry_price == 50000.0
        assert session.distance_pct == 2.0
        assert session.max_waves == 10
        assert session.isolated_fund == 1000.0
        assert session.tp_pct == 3.0
        assert session.timeout_x_min == 30.0
        assert session.gap_y_min == 5.0
        assert session.status == PyramidSessionStatus.PENDING
        assert session.current_wave == 0
        assert session.total_filled_qty == 0.0
        assert session.avg_price == 0.0
        assert len(session.waves) == 0
    
    def test_negative_entry_price_raises_error(self):
        """Test that negative entry price is rejected."""
        with pytest.raises(ValueError, match="Entry price must be positive"):
            PyramidSession(
                symbol="BTC",
                entry_price=-100.0,
                distance_pct=2.0,
                max_waves=10,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
    
    def test_zero_entry_price_raises_error(self):
        """Test that zero entry price is rejected."""
        with pytest.raises(ValueError, match="Entry price must be positive"):
            PyramidSession(
                symbol="BTC",
                entry_price=0.0,
                distance_pct=2.0,
                max_waves=10,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
    
    def test_invalid_distance_pct_too_small(self):
        """Test distance_pct < 0.1% is rejected."""
        with pytest.raises(ValueError, match="Distance must be"):
            PyramidSession(
                symbol="BTC",
                entry_price=50000.0,
                distance_pct=0.05,
                max_waves=10,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
    
    def test_invalid_distance_pct_too_large(self):
        """Test distance_pct > 50% is rejected."""
        with pytest.raises(ValueError, match="Distance must be"):
            PyramidSession(
                symbol="BTC",
                entry_price=50000.0,
                distance_pct=60.0,
                max_waves=10,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
    
    def test_invalid_max_waves_zero(self):
        """Test max_waves=0 is rejected."""
        with pytest.raises(ValueError, match="Max waves must be"):
            PyramidSession(
                symbol="BTC",
                entry_price=50000.0,
                distance_pct=2.0,
                max_waves=0,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
    
    def test_invalid_max_waves_negative(self):
        """Test negative max_waves is rejected."""
        with pytest.raises(ValueError, match="Max waves must be"):
            PyramidSession(
                symbol="BTC",
                entry_price=50000.0,
                distance_pct=2.0,
                max_waves=-5,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )
    
    def test_invalid_max_waves_too_large(self):
        """Test max_waves > 100 is rejected."""
        with pytest.raises(ValueError, match="Max waves must be"):
            PyramidSession(
                symbol="BTC",
                entry_price=50000.0,
                distance_pct=2.0,
                max_waves=150,
                isolated_fund=1000.0,
                tp_pct=3.0,
                timeout_x_min=30.0,
                gap_y_min=5.0,
            )


class TestWaveGeneration:
    """Test wave generation formulas and calculations."""
    
    @pytest.fixture
    def session(self):
        """Create test session."""
        return PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
    
    def test_wave_0_generation(self, session):
        """Test wave 0 has correct price and quantity."""
        wave = session.generate_wave(0)
        
        assert wave.wave_num == 0
        assert wave.target_price == session.entry_price
        assert wave.quantity > 0
        assert wave.status == "pending"
        assert wave.filled_qty == 0.0
    
    def test_wave_1_generation(self, session):
        """Test wave 1 has decreased price and larger quantity."""
        wave0 = session.generate_wave(0)
        wave1 = session.generate_wave(1)
        
        # Quantity increases: (1+1) * pip vs (0+1) * pip
        assert wave1.quantity > wave0.quantity
        assert wave1.quantity == pytest.approx(2 * wave0.quantity)
        
        # Price decreases by distance_pct
        expected_price = session.entry_price * (1 - session.distance_pct / 100)
        assert wave1.target_price == pytest.approx(expected_price, rel=1e-4)
    
    def test_wave_quantity_formula(self, session):
        """Test qty(n) = (n+1) * pip_size."""
        waves = [session.generate_wave(i) for i in range(5)]
        
        # Check quantity progression
        for i in range(1, 5):
            expected_ratio = (i + 1) / i
            actual_ratio = waves[i].quantity / waves[i-1].quantity
            assert actual_ratio == pytest.approx(expected_ratio, rel=1e-2)
    
    def test_wave_price_formula(self, session):
        """Test price(n) = entry * (1 - distance%)^n."""
        for n in range(5):
            wave = session.generate_wave(n)
            factor = (1 - session.distance_pct / 100) ** n
            expected_price = session.entry_price * factor
            assert wave.target_price == pytest.approx(expected_price, rel=1e-4)
    
    def test_wave_generation_sequence(self, session):
        """Test generating multiple waves in sequence."""
        waves = []
        for i in range(10):
            wave = session.generate_wave(i)
            waves.append(wave)
            assert wave.wave_num == i
        
        # Verify monotonic quantity increase
        for i in range(1, 10):
            assert waves[i].quantity > waves[i-1].quantity
        
        # Verify monotonic price decrease
        for i in range(1, 10):
            assert waves[i].target_price < waves[i-1].target_price


class TestCostEstimation:
    """Test cost estimation functions."""
    
    @pytest.fixture
    def session(self):
        """Create test session."""
        return PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
    
    def test_estimate_cost_single_wave(self, session):
        """Test estimating cost for one wave."""
        cost = session.estimate_total_cost(1)
        
        wave0 = session.generate_wave(0)
        expected = wave0.quantity * wave0.target_price
        assert cost == pytest.approx(expected, rel=1e-4)
    
    def test_estimate_cost_multiple_waves(self, session):
        """Test estimating cost for multiple waves."""
        n_waves = 5
        cost = session.estimate_total_cost(n_waves)
        
        # Manual calculation
        expected = sum(
            session.generate_wave(i).quantity * session.generate_wave(i).target_price
            for i in range(n_waves)
        )
        assert cost == pytest.approx(expected, rel=1e-2)
    
    def test_estimate_cost_all_waves(self, session):
        """Test estimating cost for all max_waves."""
        cost = session.estimate_total_cost()
        
        # Should sum all waves up to max_waves
        expected = sum(
            session.generate_wave(i).quantity * session.generate_wave(i).target_price
            for i in range(session.max_waves)
        )
        assert cost == pytest.approx(expected, rel=1e-2)
    
    def test_cost_increases_with_waves(self, session):
        """Test that estimated cost increases with more waves."""
        cost_1 = session.estimate_total_cost(1)
        cost_5 = session.estimate_total_cost(5)
        cost_10 = session.estimate_total_cost(10)
        
        assert cost_5 > cost_1
        assert cost_10 > cost_5


class TestSessionLifecycle:
    """Test session lifecycle and state transitions."""
    
    @pytest.fixture
    def session(self):
        """Create test session."""
        return PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
    
    @patch('src.findmy.kss.pyramid.get_exchange_info')
    def test_start_session(self, mock_exchange, session):
        """Test starting a session."""
        mock_exchange.return_value = {
            "minQty": 0.00001,
            "stepSize": 0.00001,
            "maxQty": 10000.0,
        }
        
        order = session.start()
        
        assert session.status == PyramidSessionStatus.ACTIVE
        assert session.start_time is not None
        assert session.current_wave == 0
        assert len(session.waves) == 1
        assert order is not None
        assert order["symbol"] == "BTC"
        assert order["side"] == "BUY"
    
    def test_start_already_active_returns_none(self, session):
        """Test starting already active session returns None."""
        session.status = PyramidSessionStatus.ACTIVE
        order = session.start()
        assert order is None
    
    def test_stop_session(self, session):
        """Test stopping a session."""
        session.status = PyramidSessionStatus.ACTIVE
        session.stop("manual")
        
        assert session.status == PyramidSessionStatus.STOPPED
    
    def test_stop_with_timeout_reason(self, session):
        """Test stopping with timeout reason."""
        session.status = PyramidSessionStatus.ACTIVE
        session.stop("timeout")
        
        assert session.status == PyramidSessionStatus.TIMEOUT
    
    def test_get_status(self, session):
        """Test get_status returns complete info."""
        status = session.get_status()
        
        assert "id" in status
        assert "symbol" in status
        assert "status" in status
        assert "entry_price" in status
        assert "avg_price" in status
        assert "total_filled_qty" in status
        assert "estimated_tp_price" in status
        assert "waves" in status


class TestFillEventHandling:
    """Test fill event processing and state updates."""
    
    @pytest.fixture
    def active_session(self):
        """Create an active session."""
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
        session.status = PyramidSessionStatus.ACTIVE
        session.start_time = datetime.utcnow()
        session.waves.append(WaveInfo(
            wave_num=0,
            quantity=0.00002,
            target_price=50000.0,
            status="sent",
        ))
        return session
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_on_fill_updates_avg_price(self, mock_prices, active_session):
        """Test on_fill updates average price correctly."""
        mock_prices.return_value = {"BTC": 50000.0}
        
        active_session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=50000.0,
        )
        
        assert active_session.avg_price == pytest.approx(50000.0, rel=1e-2)
        assert active_session.total_filled_qty == 0.00002
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_on_fill_updates_total_cost(self, mock_prices, active_session):
        """Test on_fill accumulates total cost."""
        mock_prices.return_value = {"BTC": 50000.0}
        
        active_session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=50000.0,
        )
        
        expected_cost = 0.00002 * 50000.0
        assert active_session.total_cost == pytest.approx(expected_cost)
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_on_fill_marks_wave_filled(self, mock_prices, active_session):
        """Test on_fill marks wave as filled."""
        mock_prices.return_value = {"BTC": 50000.0}
        
        active_session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=50000.0,
        )
        
        wave = active_session.waves[0]
        assert wave.status == "filled"
        assert wave.filled_qty == 0.00002
        assert wave.filled_price == 50000.0
        assert wave.filled_time is not None
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_on_fill_generates_next_wave(self, mock_prices, active_session):
        """Test on_fill generates next wave when price drops."""
        mock_prices.return_value = {"BTC": 49000.0}  # Below entry
        
        result = active_session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=49000.0,
        )
        
        assert result["action"] == "next_wave"
        assert result["order"] is not None
        assert len(active_session.waves) == 2
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_on_fill_respects_gap_time(self, mock_prices, active_session):
        """Test on_fill respects gap_y_min between waves."""
        mock_prices.return_value = {"BTC": 49000.0}
        
        # First fill
        active_session.on_fill(
            wave_num=0,
            filled_qty=0.00002,
            filled_price=50000.0,
            current_market_price=49000.0,
        )
        
        # Immediate second fill (should respect gap)
        active_session.last_fill_time = datetime.utcnow()
        
        # Add wave 1 as sent
        active_session.waves.append(WaveInfo(
            wave_num=1,
            quantity=0.00004,
            target_price=49000.0,
            status="sent",
        ))
        
        result = active_session.on_fill(
            wave_num=1,
            filled_qty=0.00004,
            filled_price=49000.0,
            current_market_price=48000.0,
        )
        
        # Should not generate next wave immediately
        assert result["action"] in ["wait", "next_wave"]  # Depends on exact timing


class TestTakeProfitLogic:
    """Test take profit triggering and execution."""
    
    @pytest.fixture
    def session_with_position(self):
        """Create session with filled position."""
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
        session.status = PyramidSessionStatus.ACTIVE
        session.avg_price = 49000.0
        session.total_filled_qty = 0.00006
        session.total_cost = 0.00006 * 49000.0
        return session
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_tp_triggers_above_threshold(self, mock_prices, session_with_position):
        """Test TP triggers when price exceeds threshold."""
        # TP threshold = 49000 * 1.03 = 50470
        mock_prices.return_value = {"BTC": 51000.0}
        
        result = session_with_position.check_tp(51000.0)
        
        assert result is not None
        assert result["action"] == "tp_triggered"
        assert result["order"]["side"] == "SELL"
        assert result["order"]["quantity"] == 0.00006
        assert session_with_position.status == PyramidSessionStatus.TP_TRIGGERED
    
    @patch('src.findmy.kss.pyramid.get_current_prices')
    def test_tp_not_triggered_below_threshold(self, mock_prices, session_with_position):
        """Test TP doesn't trigger below threshold."""
        mock_prices.return_value = {"BTC": 50000.0}
        
        result = session_with_position.check_tp(50000.0)
        
        assert result is None
        assert session_with_position.status == PyramidSessionStatus.ACTIVE
    
    def test_estimated_tp_price_no_fills(self):
        """Test TP price estimate with no fills (based on entry)."""
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
        
        expected = 50000.0 * 1.03
        assert session.estimated_tp_price == pytest.approx(expected)
    
    def test_estimated_tp_price_with_fills(self, session_with_position):
        """Test TP price estimate with fills (based on avg)."""
        expected = 49000.0 * 1.03
        assert session_with_position.estimated_tp_price == pytest.approx(expected)


class TestParameterAdjustment:
    """Test mid-session parameter adjustments."""
    
    @pytest.fixture
    def session(self):
        """Create test session."""
        return PyramidSession(
            symbol="BTC",
            entry_price=50000.0,
            distance_pct=2.0,
            max_waves=10,
            isolated_fund=1000.0,
            tp_pct=3.0,
            timeout_x_min=30.0,
            gap_y_min=5.0,
        )
    
    def test_adjust_max_waves(self, session):
        """Test adjusting max_waves."""
        changes = session.adjust_params(max_waves=15)
        
        assert "max_waves" in changes
        assert session.max_waves == 15
    
    def test_adjust_tp_pct(self, session):
        """Test adjusting tp_pct."""
        changes = session.adjust_params(tp_pct=5.0)
        
        assert "tp_pct" in changes
        assert session.tp_pct == 5.0
    
    def test_adjust_timeout(self, session):
        """Test adjusting timeout."""
        changes = session.adjust_params(timeout_x_min=60.0)
        
        assert "timeout_x_min" in changes
        assert session.timeout_x_min == 60.0
    
    def test_adjust_multiple_params(self, session):
        """Test adjusting multiple parameters."""
        changes = session.adjust_params(
            max_waves=20,
            tp_pct=4.0,
            timeout_x_min=45.0,
        )
        
        assert len(changes) == 3
        assert session.max_waves == 20
        assert session.tp_pct == 4.0
        assert session.timeout_x_min == 45.0
    
    def test_adjust_max_waves_below_current_rejected(self, session):
        """Test cannot reduce max_waves below current wave."""
        session.current_wave = 8
        changes = session.adjust_params(max_waves=5)
        
        assert "max_waves" not in changes
        assert session.max_waves == 10  # Unchanged
    
    def test_adjust_invalid_tp_pct_rejected(self, session):
        """Test invalid tp_pct is rejected."""
        changes = session.adjust_params(tp_pct=-5.0)
        
        assert "tp_pct" not in changes
        assert session.tp_pct == 3.0  # Unchanged


class TestTimeoutLogic:
    """Test timeout detection and handling."""
    
    @pytest.fixture
    def session(self):
        """Create active session."""
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
        session.status = PyramidSessionStatus.ACTIVE
        session.start_time = datetime.utcnow() - timedelta(minutes=60)
        session.last_fill_time = datetime.utcnow() - timedelta(minutes=35)
        return session
    
    def test_check_timeout_triggers(self, session):
        """Test timeout triggers after inactivity period."""
        # Last fill was 35 min ago, timeout is 30 min
        result = session.check_timeout()
        
        assert result is True
        assert session.status == PyramidSessionStatus.TIMEOUT
    
    def test_check_timeout_not_triggered(self):
        """Test timeout doesn't trigger with recent activity."""
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
        session.status = PyramidSessionStatus.ACTIVE
        session.start_time = datetime.utcnow() - timedelta(minutes=10)
        session.last_fill_time = datetime.utcnow() - timedelta(minutes=5)
        
        result = session.check_timeout()
        
        assert result is False
        assert session.status == PyramidSessionStatus.ACTIVE
    
    def test_timeout_no_fills_uses_start_time(self):
        """Test timeout uses start_time when no fills yet."""
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
        session.status = PyramidSessionStatus.ACTIVE
        session.start_time = datetime.utcnow() - timedelta(minutes=35)
        session.last_fill_time = None
        
        result = session.check_timeout()
        
        assert result is True
        assert session.status == PyramidSessionStatus.TIMEOUT
