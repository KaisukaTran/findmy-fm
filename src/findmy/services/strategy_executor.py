"""Strategy executor service that converts trading signals to orders."""

from typing import List, Dict, Any, Optional
from datetime import datetime
import logging

from findmy.strategies.base import Strategy, Signal
from findmy.services.market_data import get_current_prices, get_historical_range

logger = logging.getLogger(__name__)


class StrategyExecutor:
    """
    Executes trading strategies by:
    1. Fetching market data
    2. Generating signals from strategy
    3. Converting signals to orders
    4. Executing orders through the execution engine
    """
    
    def __init__(self, strategy: Strategy):
        """
        Initialize executor with a strategy.
        
        Args:
            strategy: Strategy instance to execute
        """
        self.strategy = strategy
        self.last_signals: List[Signal] = []
        self.last_orders: List[Dict[str, Any]] = []
    
    def fetch_market_data(
        self,
        symbols: List[str],
        start_date: datetime,
        end_date: datetime,
        timeframe: str = "1h"
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch historical market data for given symbols and date range.
        
        Args:
            symbols: List of symbols to fetch
            start_date: Start of date range
            end_date: End of date range
            timeframe: OHLCV timeframe (e.g., "1h", "4h", "1d")
        
        Returns:
            Dictionary mapping symbol -> list of OHLCV candles
        """
        market_data = {}
        
        for symbol in symbols:
            try:
                candles = get_historical_range(
                    symbol=symbol,
                    start_datetime=start_date,
                    end_datetime=end_date,
                    timeframe=timeframe
                )
                if candles:
                    market_data[symbol] = candles
                else:
                    logger.warning(f"No market data available for {symbol}")
            except Exception as e:
                logger.error(f"Failed to fetch market data for {symbol}: {e}")
                continue
        
        return market_data
    
    def run(
        self,
        start_date: datetime,
        end_date: datetime,
        timeframe: str = "1h"
    ) -> Dict[str, Any]:
        """
        Run the strategy and generate trading signals.
        
        Args:
            start_date: Start of date range for market data
            end_date: End of date range for market data
            timeframe: OHLCV timeframe (e.g., "1h", "4h", "1d")
        
        Returns:
            Dictionary with signals and execution results
        """
        # Fetch market data
        market_data = self.fetch_market_data(
            self.strategy.symbols,
            start_date,
            end_date,
            timeframe
        )
        
        if not market_data:
            return {
                "status": "error",
                "message": "No market data available for any symbols",
                "signals": [],
                "orders": []
            }
        
        # Fetch current prices for signal generation
        current_prices = get_current_prices(self.strategy.symbols)
        
        if not current_prices:
            return {
                "status": "error",
                "message": "Failed to fetch current prices",
                "signals": [],
                "orders": []
            }
        
        # Generate signals
        try:
            signals = self.strategy.generate_signals(market_data, current_prices)
            self.last_signals = signals
        except Exception as e:
            logger.error(f"Strategy signal generation failed: {e}")
            return {
                "status": "error",
                "message": f"Signal generation failed: {e}",
                "signals": [],
                "orders": []
            }
        
        # Convert signals to orders
        orders = self.signals_to_orders(signals)
        self.last_orders = orders
        
        return {
            "status": "success",
            "strategy": str(self.strategy),
            "signal_count": len(signals),
            "order_count": len(orders),
            "signals": [self._signal_to_dict(s) for s in signals],
            "orders": orders,
            "timestamp": datetime.now().isoformat()
        }
    
    def signals_to_orders(self, signals: List[Signal]) -> List[Dict[str, Any]]:
        """
        Convert trading signals to order format for execution engine.
        
        Args:
            signals: List of Signal objects from strategy
        
        Returns:
            List of order dictionaries ready for execution
        """
        orders = []
        
        for signal in signals:
            # Only process BUY and SELL signals, skip HOLD
            if signal.signal_type == "HOLD":
                continue
            
            # Create order from signal
            order = {
                "symbol": signal.symbol,
                "side": signal.signal_type,  # "BUY" or "SELL"
                "qty": self._calculate_order_size(signal),
                "price": signal.price,
                "confidence": signal.confidence,
                "timestamp": signal.timestamp.isoformat(),
                "strategy": self.strategy.name,
            }
            
            # Add additional data if present
            if signal.additional_data:
                order["additional_data"] = signal.additional_data
            
            orders.append(order)
        
        return orders
    
    def _calculate_order_size(self, signal: Signal) -> float:
        """
        Calculate order size based on signal confidence.
        
        Higher confidence -> larger order size (up to 1.0 unit)
        Lower confidence -> smaller order size
        
        Args:
            signal: Trading signal
        
        Returns:
            Order quantity (between 0.01 and 1.0)
        """
        # Base order size: 0.1 unit
        # Scale by confidence: 0.1 * (0.5 to 1.0) = 0.05 to 0.1
        min_size = 0.01
        max_size = 1.0
        
        # Confidence range: 0.0 to 1.0
        # Map to size range: min_size to max_size
        order_size = min_size + (signal.confidence * (max_size - min_size))
        
        return round(order_size, 4)
    
    def _signal_to_dict(self, signal: Signal) -> Dict[str, Any]:
        """Convert Signal object to dictionary for JSON response."""
        return {
            "symbol": signal.symbol,
            "signal_type": signal.signal_type,
            "timestamp": signal.timestamp.isoformat(),
            "confidence": signal.confidence,
            "price": signal.price,
            "additional_data": signal.additional_data or {}
        }
    
    def get_last_signals(self) -> List[Signal]:
        """Get the last generated signals."""
        return self.last_signals
    
    def get_last_orders(self) -> List[Dict[str, Any]]:
        """Get the last generated orders."""
        return self.last_orders
