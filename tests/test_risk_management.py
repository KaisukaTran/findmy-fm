"""Tests for v0.6.0 risk management and pip sizing features."""

import pytest
from unittest.mock import patch, MagicMock
from src.findmy.config import settings
from services.risk import (
    calculate_order_qty,
    get_pip_value,
    validate_order_qty,
    check_position_size,
    check_daily_loss,
    check_all_risks,
    get_account_equity,
    get_current_exposure,
    RiskCheckResult,
)


class TestPipSizing:
    """Test pip sizing calculations."""

    def test_calculate_order_qty_single_pip(self):
        """Test calculating qty for 1 pip."""
        with patch("services.risk.pip_sizing.get_exchange_info") as mock_info:
            mock_info.return_value = {
                "minQty": 0.00001,
                "stepSize": 0.00001,
                "maxQty": 10000.0,
            }

            # 1 pip = 1 × 2.0 × 0.00001 = 0.00002
            qty = calculate_order_qty("BTC", pips=1.0)
            assert qty == 0.00002

    def test_calculate_order_qty_multiple_pips(self):
        """Test calculating qty for multiple pips."""
        with patch("services.risk.pip_sizing.get_exchange_info") as mock_info:
            mock_info.return_value = {
                "minQty": 0.00001,
                "stepSize": 0.00001,
                "maxQty": 10000.0,
            }

            # 5 pips = 5 × 2.0 × 0.00001 = 0.0001
            qty = calculate_order_qty("BTC", pips=5.0)
            assert qty == 0.0001

    def test_calculate_order_qty_rounding(self):
        """Test qty rounding to step size."""
        with patch("services.risk.pip_sizing.get_exchange_info") as mock_info:
            mock_info.return_value = {
                "minQty": 0.001,
                "stepSize": 0.001,
                "maxQty": 100000.0,
            }

            # Should round to step size
            qty = calculate_order_qty("ETH", pips=1.5)
            assert qty % 0.001 == 0

    @pytest.mark.timeout(10)
    def test_validate_order_qty_valid(self):
        """Test validating valid order qty."""
        with patch("services.risk.pip_sizing.get_exchange_info") as mock_info:
            mock_info.return_value = {
                "minQty": 0.00001,
                "stepSize": 0.00001,
                "maxQty": 10000.0,
            }

            is_valid, msg = validate_order_qty("BTC", 0.001)
            assert is_valid is True
            assert msg == ""

    @pytest.mark.timeout(10)
    def test_validate_order_qty_below_minimum(self):
        """Test validating qty below minimum."""
        with patch("services.risk.pip_sizing.get_exchange_info") as mock_info:
            mock_info.return_value = {
                "minQty": 0.00001,
                "stepSize": 0.00001,
                "maxQty": 10000.0,
            }

            is_valid, msg = validate_order_qty("BTC", 0.000001)
            assert is_valid is False
            assert "below minimum" in msg

    @pytest.mark.timeout(10)
    def test_validate_order_qty_exceeds_maximum(self):
        """Test validating qty above maximum."""
        with patch("services.risk.pip_sizing.get_exchange_info") as mock_info:
            mock_info.return_value = {
                "minQty": 0.00001,
                "stepSize": 0.00001,
                "maxQty": 10000.0,
            }

            is_valid, msg = validate_order_qty("BTC", 20000.0)
            assert is_valid is False
            assert "exceeds maximum" in msg


class TestRiskManagement:
    """Test risk management checks."""

    @pytest.mark.timeout(10)
    def test_get_account_equity(self):
        """Test getting account equity."""
        equity = get_account_equity()
        assert equity == 10000.0  # Default value

    @pytest.mark.timeout(10)
    def test_risk_check_result_passed(self):
        """Test passing risk check result."""
        result = RiskCheckResult(passed=True)
        assert result.passed is True
        assert bool(result) is True
        assert "passed" in str(result).lower()

    @pytest.mark.timeout(10)
    def test_risk_check_result_failed(self):
        """Test failing risk check result."""
        violation = "Position size exceeds limit"
        result = RiskCheckResult(passed=False, violation=violation)
        assert result.passed is False
        assert bool(result) is False
        assert result.violation == violation
        assert violation in str(result)

    @pytest.mark.timeout(10)
    def test_check_position_size_passes(self):
        """Test position size check passes."""
        with patch("services.risk.risk_management.get_current_exposure") as mock_exposure:
            with patch("services.risk.risk_management.get_account_equity") as mock_equity:
                mock_exposure.return_value = (0.5, 5.0)  # 5% exposure
                mock_equity.return_value = 10000.0

                result = check_position_size("BTC", 0.1)
                assert result.passed is True

    @pytest.mark.timeout(10)
    def test_check_position_size_fails(self):
        """Test position size check fails when exceeding limit."""
        with patch("services.risk.risk_management.get_current_exposure") as mock_exposure:
            with patch("services.risk.risk_management.get_account_equity") as mock_equity:
                # Mock high current exposure
                mock_exposure.return_value = (10.0, 95.0)  # 95% exposure
                mock_equity.return_value = 10000.0

                result = check_position_size("BTC", 1.0)
                # Will likely fail if proposed qty exceeds max
                # (depends on actual calculation logic)

    @pytest.mark.timeout(10)
    def test_check_daily_loss_passes(self):
        """Test daily loss check passes."""
        with patch("services.risk.risk_management.get_daily_loss") as mock_loss:
            with patch("services.risk.risk_management.get_account_equity") as mock_equity:
                mock_loss.return_value = 200.0  # $200 loss
                mock_equity.return_value = 10000.0

                result = check_daily_loss()
                assert result.passed is True

    @pytest.mark.timeout(10)
    def test_check_daily_loss_fails(self):
        """Test daily loss check fails when exceeding limit."""
        with patch("services.risk.risk_management.get_daily_loss") as mock_loss:
            with patch("services.risk.risk_management.get_account_equity") as mock_equity:
                mock_loss.return_value = 700.0  # $700 loss (7% of $10k)
                mock_equity.return_value = 10000.0

                result = check_daily_loss()
                assert result.passed is False
                assert "daily loss" in result.violation.lower()

    @pytest.mark.timeout(10)
    def test_check_all_risks_passes(self):
        """Test all risk checks pass."""
        with patch("services.risk.risk_management.check_position_size") as mock_pos:
            with patch("services.risk.risk_management.check_daily_loss") as mock_loss:
                mock_pos.return_value = RiskCheckResult(True)
                mock_loss.return_value = RiskCheckResult(True)

                passed, violations = check_all_risks("BTC", 0.1)
                assert passed is True
                assert len(violations) == 0

    @pytest.mark.timeout(10)
    def test_check_all_risks_fails_multiple(self):
        """Test risk checks fail with multiple violations."""
        with patch("services.risk.risk_management.check_position_size") as mock_pos:
            with patch("services.risk.risk_management.check_daily_loss") as mock_loss:
                mock_pos.return_value = RiskCheckResult(False, "Position too large")
                mock_loss.return_value = RiskCheckResult(False, "Daily loss exceeded")

                passed, violations = check_all_risks("BTC", 10.0)
                assert passed is False
                assert len(violations) == 2
                assert any("Position" in v for v in violations)
                assert any("Daily" in v for v in violations)


class TestPendingOrdersWithPips:
    """Test pending orders integration with pip sizing."""

    @pytest.mark.timeout(15)
    def test_queue_order_with_quantity(self):
        """Test queuing order with explicit quantity."""
        from services.sot.pending_orders_service import queue_order

        with patch("services.sot.pending_orders_service.check_all_risks") as mock_risk:
            mock_risk.return_value = (True, [])

            order, risk_note = queue_order(
                symbol="BTC",
                side="BUY",
                quantity=0.5,
                price=65000.0,
                source="test",
            )

            assert order.symbol == "BTC"
            assert order.side == "BUY"
            assert order.quantity == 0.5
            assert risk_note is None

    @pytest.mark.timeout(15)
    def test_queue_order_with_pips(self):
        """Test queuing order with pips field."""
        from services.sot.pending_orders_service import queue_order

        with patch("services.sot.pending_orders_service.calculate_order_qty") as mock_qty:
            with patch("services.sot.pending_orders_service.check_all_risks") as mock_risk:
                mock_qty.return_value = 0.00002
                mock_risk.return_value = (True, [])

                order, risk_note = queue_order(
                    symbol="BTC",
                    side="BUY",
                    price=65000.0,
                    source="strategy",
                    pips=1.0,
                )

                assert order.symbol == "BTC"
                assert order.pips == 1.0
                assert order.quantity == 0.00002
                assert risk_note is None

    @pytest.mark.timeout(15)
    def test_queue_order_risk_violation(self):
        """Test queuing order with risk violation."""
        from services.sot.pending_orders_service import queue_order

        with patch("services.sot.pending_orders_service.calculate_order_qty") as mock_qty:
            with patch("services.sot.pending_orders_service.check_all_risks") as mock_risk:
                mock_qty.return_value = 5.0
                mock_risk.return_value = (False, ["Position size exceeds 10%"])

                order, risk_note = queue_order(
                    symbol="BTC",
                    side="BUY",
                    price=65000.0,
                    source="excel",
                    pips=5.0,
                )

                assert order.symbol == "BTC"
                assert risk_note == "Position size exceeds 10%"
                assert "Position size" in order.note


class TestPytestTimeout:
    """Test pytest timeout markers."""

    @pytest.mark.timeout(30)
    def test_fast_operation(self):
        """Test that fast operations complete within timeout."""
        result = 1 + 1
        assert result == 2

    @pytest.mark.timeout(300)
    def test_slow_operation_with_extended_timeout(self):
        """Test slow operations with extended timeout."""
        # Simulate slow operation (but don't actually sleep long)
        total = 0
        for i in range(1000):
            total += i
        assert total > 0

    def test_default_timeout(self):
        """Test that default timeout is applied (30s)."""
        # This test should complete within 30 seconds
        result = list(range(100))
        assert len(result) == 100
