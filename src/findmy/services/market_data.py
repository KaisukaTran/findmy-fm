"""Market data service for fetching real-time and historical prices from Binance."""

import time
from typing import Optional
from datetime import datetime, timedelta

import ccxt


class BinancePriceCache:
    """Simple in-memory cache for Binance prices with TTL."""

    def __init__(self, ttl_seconds: int = 60):
        """Initialize cache with TTL in seconds."""
        self.ttl_seconds = ttl_seconds
        self.prices: dict[str, float] = {}
        self.last_update: float = 0

    def is_valid(self) -> bool:
        """Check if cache is still valid."""
        return time.time() - self.last_update < self.ttl_seconds

    def get(self, symbol: str) -> Optional[float]:
        """Get cached price if valid."""
        if self.is_valid():
            return self.prices.get(symbol)
        return None

    def set(self, prices: dict[str, float]) -> None:
        """Update cache with new prices."""
        self.prices = prices
        self.last_update = time.time()

    def clear(self) -> None:
        """Clear cache."""
        self.prices = {}
        self.last_update = 0


# Global cache instance
_price_cache = BinancePriceCache(ttl_seconds=60)


def get_current_prices(symbols: list[str]) -> dict[str, float]:
    """
    Fetch current prices from Binance for given symbols.

    Args:
        symbols: List of base currency symbols (e.g., ["BTC", "ETH"])

    Returns:
        Dictionary mapping symbol to current USD price (e.g., {"BTC": 65000.0})

    Note:
        Uses in-memory cache to avoid rate limits. Cache TTL is 60 seconds.
        If Binance is unavailable, returns last known prices or empty dict.
    """
    if not symbols:
        return {}

    # Check cache first
    cached_prices = {}
    missing_symbols = []

    for symbol in symbols:
        cached_price = _price_cache.get(symbol)
        if cached_price is not None:
            cached_prices[symbol] = cached_price
        else:
            missing_symbols.append(symbol)

    # If all symbols are cached, return them
    if not missing_symbols:
        return cached_prices

    # Fetch missing symbols from Binance
    try:
        exchange = ccxt.binance()
        fetched_prices = {}

        for symbol in missing_symbols:
            # Format as BTC/USDT for Binance
            pair = f"{symbol}/USDT"
            try:
                ticker = exchange.fetch_ticker(pair)
                price = ticker["last"]
                fetched_prices[symbol] = float(price)
            except Exception:
                # If single symbol fails, continue with others
                continue

        # Update cache with fetched prices
        if fetched_prices:
            all_prices = {**cached_prices, **fetched_prices}
            _price_cache.set(all_prices)
            return all_prices

    except Exception as e:
        # If Binance fetch fails, return cached prices
        pass

    return cached_prices


def get_unrealized_pnl(
    symbol: str, quantity: float, avg_price: float, current_price: Optional[float] = None
) -> tuple[float, float]:
    """
    Calculate unrealized PnL for a position.

    Args:
        symbol: Asset symbol
        quantity: Position quantity
        avg_price: Average entry price
        current_price: Current market price (fetched if None)

    Returns:
        Tuple of (unrealized_pnl, market_value)
    """
    if current_price is None:
        prices = get_current_prices([symbol])
        current_price = prices.get(symbol)
        if current_price is None:
            return 0.0, 0.0

    market_value = quantity * current_price
    unrealized_pnl = market_value - (quantity * avg_price)
    return unrealized_pnl, market_value


def clear_cache() -> None:
    """Clear the price cache (useful for testing)."""
    _price_cache.clear()


# ============================================================
# HISTORICAL DATA FOR BACKTESTING
# ============================================================


def get_historical_ohlcv(
    symbol: str, timeframe: str = "1h", limit: int = 100
) -> list[dict[str, any]]:
    """
    Fetch historical OHLCV data from Binance for backtesting.

    Args:
        symbol: Base currency symbol (e.g., "BTC", "ETH")
        timeframe: OHLCV timeframe (e.g., "1m", "5m", "1h", "4h", "1d")
        limit: Number of candles to fetch (max ~500-1000 depending on exchange)

    Returns:
        List of OHLCV candles: [timestamp_ms, open, high, low, close, volume]
        Each candle is [timestamp, open, high, low, close, volume]
    """
    try:
        exchange = ccxt.binance()
        pair = f"{symbol}/USDT"

        # Fetch OHLCV data
        ohlcv = exchange.fetch_ohlcv(pair, timeframe, limit=limit)

        # Transform to list of dicts for easier handling
        result = []
        for candle in ohlcv:
            timestamp_ms, open_price, high, low, close, volume = candle
            result.append(
                {
                    "timestamp": timestamp_ms,
                    "timestamp_dt": datetime.fromtimestamp(timestamp_ms / 1000),
                    "open": float(open_price),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": float(volume),
                }
            )

        return result

    except Exception as e:
        # Return empty list if fetch fails
        return []


def get_historical_range(
    symbol: str,
    start_datetime: datetime,
    end_datetime: datetime,
    timeframe: str = "1h",
) -> list[dict[str, any]]:
    """
    Fetch historical OHLCV data for a specific date range.

    Args:
        symbol: Base currency symbol (e.g., "BTC", "ETH")
        start_datetime: Start of date range
        end_datetime: End of date range
        timeframe: OHLCV timeframe (e.g., "1m", "5m", "1h", "4h", "1d")

    Returns:
        List of OHLCV candles within the date range
    """
    try:
        exchange = ccxt.binance()
        pair = f"{symbol}/USDT"

        # Estimate number of candles needed
        timeframe_minutes = {
            "1m": 1,
            "5m": 5,
            "15m": 15,
            "30m": 30,
            "1h": 60,
            "4h": 240,
            "1d": 1440,
        }
        minutes = timeframe_minutes.get(timeframe, 60)
        date_range_minutes = int((end_datetime - start_datetime).total_seconds() / 60)
        estimated_candles = min(date_range_minutes // minutes + 10, 1000)

        # Fetch data starting from start_datetime
        ohlcv = exchange.fetch_ohlcv(
            pair,
            timeframe,
            since=int(start_datetime.timestamp() * 1000),
            limit=estimated_candles,
        )

        # Filter to date range and transform
        result = []
        for candle in ohlcv:
            timestamp_ms, open_price, high, low, close, volume = candle
            candle_dt = datetime.fromtimestamp(timestamp_ms / 1000)

            # Only include candles within range
            if start_datetime <= candle_dt <= end_datetime:
                result.append(
                    {
                        "timestamp": timestamp_ms,
                        "timestamp_dt": candle_dt,
                        "open": float(open_price),
                        "high": float(high),
                        "low": float(low),
                        "close": float(close),
                        "volume": float(volume),
                    }
                )

        return result

    except Exception as e:
        return []
