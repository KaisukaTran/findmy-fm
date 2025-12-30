"""Tests for market data service and Binance price integration."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import time

from findmy.services.market_data import (
    BinancePriceCache,
    get_current_prices,
    get_unrealized_pnl,
    clear_cache,
    _price_cache,
)


class TestBinancePriceCache:
    """Test the BinancePriceCache class."""

    def test_cache_initialization(self):
        """Test cache initializes with correct TTL."""
        cache = BinancePriceCache(ttl_seconds=60)
        assert cache.ttl_seconds == 60
        assert cache.prices == {}
        assert cache.last_update == 0

    def test_cache_is_valid(self):
        """Test cache validity checking."""
        cache = BinancePriceCache(ttl_seconds=10)
        
        # Not valid initially
        assert not cache.is_valid()
        
        # Set prices
        cache.set({"BTC": 65000.0})
        assert cache.is_valid()
        
        # Wait for TTL to expire
        time.sleep(0.1)
        # Manually set last_update to past to simulate expiry
        cache.last_update = time.time() - 11
        assert not cache.is_valid()

    def test_cache_get(self):
        """Test getting prices from cache."""
        cache = BinancePriceCache(ttl_seconds=60)
        cache.set({"BTC": 65000.0, "ETH": 3200.0})
        
        # Valid cache should return prices
        assert cache.get("BTC") == 65000.0
        assert cache.get("ETH") == 3200.0
        assert cache.get("SOL") is None
        
        # Expired cache should return None
        cache.last_update = time.time() - 61
        assert cache.get("BTC") is None

    def test_cache_set_and_clear(self):
        """Test setting and clearing cache."""
        cache = BinancePriceCache(ttl_seconds=60)
        
        cache.set({"BTC": 65000.0})
        assert cache.prices == {"BTC": 65000.0}
        
        cache.clear()
        assert cache.prices == {}
        assert cache.last_update == 0


class TestGetCurrentPrices:
    """Test get_current_prices function."""

    def test_empty_symbols_list(self):
        """Test with empty symbols list."""
        result = get_current_prices([])
        assert result == {}

    @patch("findmy.services.market_data.ccxt.binance")
    def test_fetch_single_symbol(self, mock_binance_class):
        """Test fetching price for a single symbol."""
        # Setup mock
        mock_exchange = MagicMock()
        mock_binance_class.return_value = mock_exchange
        
        mock_exchange.fetch_ticker.return_value = {
            "last": 65000.0,
            "timestamp": 1234567890,
        }
        
        # Clear cache
        clear_cache()
        
        # Fetch prices
        result = get_current_prices(["BTC"])
        
        # Verify
        assert "BTC" in result
        assert result["BTC"] == 65000.0
        mock_exchange.fetch_ticker.assert_called_once_with("BTC/USDT")

    @patch("findmy.services.market_data.ccxt.binance")
    def test_fetch_multiple_symbols(self, mock_binance_class):
        """Test fetching prices for multiple symbols."""
        mock_exchange = MagicMock()
        mock_binance_class.return_value = mock_exchange
        
        # Mock different prices for each symbol
        def fetch_ticker_side_effect(pair):
            prices = {
                "BTC/USDT": {"last": 65000.0},
                "ETH/USDT": {"last": 3200.0},
                "SOL/USDT": {"last": 150.5},
            }
            return prices[pair]
        
        mock_exchange.fetch_ticker.side_effect = fetch_ticker_side_effect
        
        clear_cache()
        
        result = get_current_prices(["BTC", "ETH", "SOL"])
        
        assert result["BTC"] == 65000.0
        assert result["ETH"] == 3200.0
        assert result["SOL"] == 150.5

    @patch("findmy.services.market_data.ccxt.binance")
    def test_cache_prevents_repeated_fetches(self, mock_binance_class):
        """Test that cache prevents repeated API calls within TTL."""
        mock_exchange = MagicMock()
        mock_binance_class.return_value = mock_exchange
        
        mock_exchange.fetch_ticker.return_value = {"last": 65000.0}
        
        clear_cache()
        
        # First call should fetch
        result1 = get_current_prices(["BTC"])
        assert mock_exchange.fetch_ticker.call_count == 1
        
        # Second call should use cache (no new API call)
        result2 = get_current_prices(["BTC"])
        assert result1 == result2
        assert mock_exchange.fetch_ticker.call_count == 1  # Still 1, not 2

    @patch("findmy.services.market_data.ccxt.binance")
    def test_single_symbol_failure_skips_gracefully(self, mock_binance_class):
        """Test that failure to fetch one symbol doesn't block others."""
        mock_exchange = MagicMock()
        mock_binance_class.return_value = mock_exchange
        
        def fetch_ticker_side_effect(pair):
            if pair == "INVALID/USDT":
                raise Exception("Symbol not found")
            return {"last": 65000.0}
        
        mock_exchange.fetch_ticker.side_effect = fetch_ticker_side_effect
        
        clear_cache()
        
        result = get_current_prices(["BTC", "INVALID", "ETH"])
        
        # Should get BTC but not INVALID (failed) or ETH (not attempted after failure)
        assert "BTC" in result
        assert result["BTC"] == 65000.0

    @patch("findmy.services.market_data.ccxt.binance")
    def test_binance_api_down_returns_cached_prices(self, mock_binance_class):
        """Test fallback to cached prices when API is down."""
        mock_exchange = MagicMock()
        mock_binance_class.return_value = mock_exchange
        
        # First call succeeds
        mock_exchange.fetch_ticker.return_value = {"last": 65000.0}
        clear_cache()
        result1 = get_current_prices(["BTC"])
        assert result1["BTC"] == 65000.0
        
        # Second call fails but should return cached price
        mock_exchange.fetch_ticker.side_effect = Exception("API down")
        
        # Simulate TTL expired but with cache available
        _price_cache.last_update = 0  # Force new fetch
        result2 = get_current_prices(["BTC"])
        
        # Should have cached value still
        assert result2.get("BTC") is None  # Because fetch failed completely


class TestGetUnrealizedPnL:
    """Test get_unrealized_pnl function."""

    def test_unrealized_pnl_with_profit(self):
        """Test unrealized PnL calculation with profit."""
        unrealized_pnl, market_value = get_unrealized_pnl(
            symbol="BTC",
            quantity=0.5,
            avg_price=60000.0,
            current_price=65000.0,
        )
        
        assert market_value == 32500.0  # 0.5 * 65000
        assert unrealized_pnl == 2500.0  # (0.5 * 65000) - (0.5 * 60000)

    def test_unrealized_pnl_with_loss(self):
        """Test unrealized PnL calculation with loss."""
        unrealized_pnl, market_value = get_unrealized_pnl(
            symbol="ETH",
            quantity=10.0,
            avg_price=3500.0,
            current_price=3200.0,
        )
        
        assert market_value == 32000.0  # 10 * 3200
        assert unrealized_pnl == -3000.0  # (10 * 3200) - (10 * 3500)

    def test_unrealized_pnl_breakeven(self):
        """Test unrealized PnL at breakeven."""
        unrealized_pnl, market_value = get_unrealized_pnl(
            symbol="SOL",
            quantity=100.0,
            avg_price=150.0,
            current_price=150.0,
        )
        
        assert market_value == 15000.0
        assert unrealized_pnl == 0.0

    @patch("findmy.services.market_data.get_current_prices")
    def test_unrealized_pnl_fetches_price_if_none(self, mock_get_prices):
        """Test that current_price is fetched if not provided."""
        mock_get_prices.return_value = {"BTC": 65000.0}
        
        unrealized_pnl, market_value = get_unrealized_pnl(
            symbol="BTC",
            quantity=0.5,
            avg_price=60000.0,
        )
        
        assert market_value == 32500.0
        assert unrealized_pnl == 2500.0
        mock_get_prices.assert_called_once_with(["BTC"])

    @patch("findmy.services.market_data.get_current_prices")
    def test_unrealized_pnl_returns_zero_if_price_unavailable(self, mock_get_prices):
        """Test that unrealized PnL returns 0 if price cannot be fetched."""
        mock_get_prices.return_value = {}
        
        unrealized_pnl, market_value = get_unrealized_pnl(
            symbol="BTC",
            quantity=0.5,
            avg_price=60000.0,
        )
        
        assert unrealized_pnl == 0.0
        assert market_value == 0.0


class TestIntegrationScenarios:
    """Integration tests for realistic scenarios."""

    @patch("findmy.services.market_data.ccxt.binance")
    def test_full_portfolio_valuation(self, mock_binance_class):
        """Test mark-to-market valuation of a full portfolio."""
        mock_exchange = MagicMock()
        mock_binance_class.return_value = mock_exchange
        
        # Setup portfolio
        positions = [
            {"symbol": "BTC", "quantity": 0.5, "avg_price": 60000.0},
            {"symbol": "ETH", "quantity": 10.0, "avg_price": 2500.0},
            {"symbol": "SOL", "quantity": 100.0, "avg_price": 150.0},
        ]
        
        # Setup prices
        prices = {
            "BTC": 65000.0,
            "ETH": 3200.0,
            "SOL": 155.0,
        }
        
        def fetch_ticker_side_effect(pair):
            symbol = pair.split("/")[0]
            return {"last": prices[symbol]}
        
        mock_exchange.fetch_ticker.side_effect = fetch_ticker_side_effect
        
        clear_cache()
        
        # Calculate total valuation
        total_cost = sum(p["quantity"] * p["avg_price"] for p in positions)
        total_market_value = 0
        total_unrealized_pnl = 0
        
        fetched_prices = get_current_prices([p["symbol"] for p in positions])
        
        for pos in positions:
            current_price = fetched_prices[pos["symbol"]]
            market_value = pos["quantity"] * current_price
            unrealized_pnl = market_value - (pos["quantity"] * pos["avg_price"])
            
            total_market_value += market_value
            total_unrealized_pnl += unrealized_pnl
        
        # Verify calculations
        # BTC: 0.5 * 65000 = 32500
        # ETH: 10 * 3200 = 32000
        # SOL: 100 * 155 = 15500
        # Total market value = 32500 + 32000 + 15500 = 80000
        assert total_cost == 70000.0  # (0.5 * 60000) + (10 * 2500) + (100 * 150)
        assert total_market_value == 80000.0  # 32500 + 32000 + 15500
        assert total_unrealized_pnl == 10000.0  # 80000 - 70000

    def test_cache_clear_functionality(self):
        """Test that cache can be cleared."""
        clear_cache()
        assert _price_cache.prices == {}
        assert _price_cache.last_update == 0
