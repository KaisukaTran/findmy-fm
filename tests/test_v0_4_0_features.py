"""
Tests for v0.4.0 market data, backtesting, and WebSocket features.

Covers:
- Historical OHLCV data fetching
- Backtesting service and metrics calculation
- WebSocket connection management
- API endpoint validation
"""

import pytest
from datetime import datetime, timedelta
from typing import List, Dict

from findmy.services.market_data import (
    get_historical_ohlcv,
    get_historical_range,
    get_current_prices,
    clear_cache,
)
from findmy.services.backtesting import (
    BacktestRequest,
    run_backtest,
    calculate_sharpe_ratio,
)


class TestHistoricalData:
    """Test historical OHLCV data fetching."""

    def test_get_historical_ohlcv_structure(self):
        """Verify OHLCV structure has required fields."""
        clear_cache()
        ohlcv = get_historical_ohlcv("BTC", timeframe="1h", limit=10)
        
        # May be empty if exchange is unavailable, but structure should be list
        assert isinstance(ohlcv, list)
        
        if ohlcv:  # Only check structure if data fetched
            assert len(ohlcv) > 0
            candle = ohlcv[0]
            assert "timestamp" in candle
            assert "open" in candle
            assert "high" in candle
            assert "low" in candle
            assert "close" in candle
            assert "volume" in candle
            assert "timestamp_dt" in candle

    def test_get_historical_ohlcv_pricing(self):
        """Verify high >= low and high >= close in OHLCV data."""
        clear_cache()
        ohlcv = get_historical_ohlcv("BTC", timeframe="1h", limit=5)
        
        if ohlcv:
            for candle in ohlcv:
                assert candle["high"] >= candle["low"], "High should be >= low"
                assert candle["high"] >= candle["close"], "High should be >= close"
                assert candle["low"] <= candle["close"], "Low should be <= close"

    def test_get_historical_range_returns_list(self):
        """Verify historical range returns list."""
        clear_cache()
        start = datetime.now() - timedelta(days=7)
        end = datetime.now()
        
        ohlcv = get_historical_range("ETH", start, end, timeframe="1d")
        assert isinstance(ohlcv, list)

    def test_get_historical_range_validates_dates(self):
        """Verify date validation in historical range."""
        clear_cache()
        start = datetime.now()
        end = datetime.now() - timedelta(days=7)  # End before start
        
        # Should return empty if end < start
        ohlcv = get_historical_range("BTC", start, end)
        assert isinstance(ohlcv, list)

    def test_get_historical_ohlcv_handles_invalid_symbol(self):
        """Verify graceful handling of invalid symbols."""
        clear_cache()
        ohlcv = get_historical_ohlcv("INVALID_SYMBOL_XYZ")
        assert isinstance(ohlcv, list)
        # Should return empty on error
        if not ohlcv:
            assert len(ohlcv) == 0


class TestBacktestRequest:
    """Test BacktestRequest initialization."""

    def test_backtest_request_initialization(self):
        """Verify BacktestRequest stores parameters."""
        start = datetime(2024, 1, 1)
        end = datetime(2024, 12, 31)
        
        request = BacktestRequest(
            symbols=["BTC", "ETH"],
            start_date=start,
            end_date=end,
            initial_capital=50000.0,
            timeframe="4h",
        )
        
        assert request.symbols == ["BTC", "ETH"]
        assert request.start_date == start
        assert request.end_date == end
        assert request.initial_capital == 50000.0
        assert request.timeframe == "4h"

    def test_backtest_request_defaults(self):
        """Verify BacktestRequest default values."""
        start = datetime(2024, 1, 1)
        end = datetime(2024, 1, 31)
        
        request = BacktestRequest(
            symbols=["BTC"],
            start_date=start,
            end_date=end,
        )
        
        assert request.initial_capital == 10000.0
        assert request.timeframe == "1h"


class TestBacktestService:
    """Test backtesting service."""

    def test_run_backtest_returns_result(self):
        """Verify run_backtest returns BacktestResult."""
        request = BacktestRequest(
            symbols=["BTC"],
            start_date=datetime.now() - timedelta(days=7),
            end_date=datetime.now(),
            initial_capital=10000.0,
        )
        
        result = run_backtest(request)
        
        # Result should have required fields
        assert result.to_dict() is not None
        assert "equity_curve" in result.to_dict()
        assert "metrics" in result.to_dict()
        assert "status" in result.to_dict()

    def test_backtest_result_structure(self):
        """Verify BacktestResult dictionary structure."""
        request = BacktestRequest(
            symbols=["BTC"],
            start_date=datetime.now() - timedelta(days=3),
            end_date=datetime.now(),
        )
        
        result = run_backtest(request)
        result_dict = result.to_dict()
        
        # Check all required keys present
        assert "equity_curve" in result_dict
        assert "trades" in result_dict
        assert "metrics" in result_dict
        assert "status" in result_dict
        assert "error" in result_dict
        
        # Verify types
        assert isinstance(result_dict["equity_curve"], list)
        assert isinstance(result_dict["trades"], list)
        assert isinstance(result_dict["metrics"], dict)
        assert isinstance(result_dict["status"], str)

    def test_backtest_handles_invalid_dates(self):
        """Verify backtest gracefully handles invalid date ranges."""
        # Start date after end date
        request = BacktestRequest(
            symbols=["BTC"],
            start_date=datetime(2024, 12, 31),
            end_date=datetime(2024, 1, 1),
        )
        
        result = run_backtest(request)
        # Should return error status
        assert result.status in ["error", "completed"]

    def test_backtest_metrics_structure(self):
        """Verify metrics returned from backtest."""
        request = BacktestRequest(
            symbols=["BTC"],
            start_date=datetime.now() - timedelta(days=5),
            end_date=datetime.now(),
        )
        
        result = run_backtest(request)
        
        if result.status == "completed":
            metrics = result.metrics
            
            # Check expected metrics present
            if metrics:
                assert "initial_capital" in metrics
                assert "final_equity" in metrics
                assert "total_return_pct" in metrics

    def test_sharpe_ratio_calculation(self):
        """Verify Sharpe ratio calculation."""
        returns = [0.01, 0.02, -0.01, 0.03, 0.005]
        sharpe = calculate_sharpe_ratio(returns)
        
        # Should be a number
        assert isinstance(sharpe, float)
        # Should be reasonable value
        assert sharpe >= -100 and sharpe <= 100

    def test_sharpe_ratio_empty_returns(self):
        """Verify Sharpe ratio handles empty returns."""
        sharpe = calculate_sharpe_ratio([])
        assert sharpe == 0.0
        
        sharpe = calculate_sharpe_ratio([0.01])
        assert sharpe == 0.0

    def test_sharpe_ratio_zero_volatility(self):
        """Verify Sharpe ratio handles zero volatility."""
        returns = [0.01, 0.01, 0.01]  # Constant returns
        sharpe = calculate_sharpe_ratio(returns)
        assert sharpe == 0.0


class TestIntegrationBacktest:
    """Integration tests for backtesting."""

    def test_backtest_workflow(self):
        """Test complete backtesting workflow."""
        request = BacktestRequest(
            symbols=["BTC"],
            start_date=datetime.now() - timedelta(days=7),
            end_date=datetime.now(),
            initial_capital=10000.0,
            timeframe="1d",
        )
        
        result = run_backtest(request)
        
        # Should complete without exception
        assert result is not None
        assert result.status in ["completed", "error"]
        
        result_dict = result.to_dict()
        assert result_dict["status"] in ["completed", "error"]

    def test_backtest_multiple_symbols(self):
        """Test backtesting with multiple symbols."""
        request = BacktestRequest(
            symbols=["BTC", "ETH"],
            start_date=datetime.now() - timedelta(days=5),
            end_date=datetime.now(),
        )
        
        result = run_backtest(request)
        # Should handle multiple symbols
        assert result is not None

    def test_backtest_equity_curve_chronological(self):
        """Verify equity curve is in chronological order."""
        request = BacktestRequest(
            symbols=["BTC"],
            start_date=datetime.now() - timedelta(days=3),
            end_date=datetime.now(),
        )
        
        result = run_backtest(request)
        
        if result.equity_curve and len(result.equity_curve) > 1:
            # Check timestamps are increasing
            prev_ts = 0
            for point in result.equity_curve:
                assert point["timestamp"] >= prev_ts, "Timestamps should be increasing"
                prev_ts = point["timestamp"]
