"""Moving Average Crossover strategy implementation."""

from datetime import datetime
from typing import Dict, List, Any

from .base import Strategy, Signal


class MovingAverageStrategy(Strategy):
    """
    Simple Moving Average Crossover Strategy.
    
    Generates BUY signals when fast MA > slow MA (bullish crossover)
    Generates SELL signals when fast MA < slow MA (bearish crossover)
    Generates HOLD otherwise
    
    Configuration:
        - fast_period: Fast MA period (default: 9)
        - slow_period: Slow MA period (default: 21)
        - min_confidence: Minimum confidence threshold (default: 0.6)
    """
    
    def __init__(self, symbols: List[str], config: Dict[str, Any] = None):
        """
        Initialize Moving Average Strategy.
        
        Args:
            symbols: List of symbols to trade
            config: Configuration with fast_period, slow_period, min_confidence
        """
        default_config = {
            "fast_period": 9,
            "slow_period": 21,
            "min_confidence": 0.6,
        }
        
        if config:
            default_config.update(config)
        
        super().__init__(
            name="Moving Average Crossover",
            symbols=symbols,
            config=default_config
        )
    
    def calculate_sma(self, prices: List[float], period: int) -> float:
        """
        Calculate Simple Moving Average.
        
        Args:
            prices: List of prices (close prices from candles)
            period: Number of periods for MA
        
        Returns:
            Simple moving average value
        """
        if len(prices) < period:
            raise ValueError(f"Need at least {period} prices, got {len(prices)}")
        
        return sum(prices[-period:]) / period
    
    def generate_signals(
        self,
        market_data: Dict[str, List[Dict[str, Any]]],
        current_prices: Dict[str, float],
    ) -> List[Signal]:
        """
        Generate trading signals using MA crossover.
        
        Args:
            market_data: Dictionary of symbol -> list of OHLCV candles
            current_prices: Dictionary of symbol -> current price
        
        Returns:
            List of Signal objects
        """
        signals = []
        
        # Validate market data
        if not self.validate_market_data(market_data):
            return signals
        
        fast_period = self.config.get("fast_period", 9)
        slow_period = self.config.get("slow_period", 21)
        min_confidence = self.config.get("min_confidence", 0.6)
        
        for symbol in self.symbols:
            candles = market_data.get(symbol, [])
            
            # Need at least slow_period candles for calculation
            if len(candles) < slow_period:
                continue
            
            # Extract close prices
            close_prices = [candle["close"] for candle in candles]
            
            try:
                # Calculate MAs
                fast_ma = self.calculate_sma(close_prices, fast_period)
                slow_ma = self.calculate_sma(close_prices, slow_period)
                
                # Get current price
                current_price = current_prices.get(symbol)
                if current_price is None:
                    continue
                
                # Determine signal
                if fast_ma > slow_ma:
                    signal_type = "BUY"
                    # Confidence based on how much fast MA exceeds slow MA
                    ma_diff_pct = abs(fast_ma - slow_ma) / slow_ma
                    confidence = min(0.95, min_confidence + ma_diff_pct)
                elif fast_ma < slow_ma:
                    signal_type = "SELL"
                    # Confidence based on how much slow MA exceeds fast MA
                    ma_diff_pct = abs(slow_ma - fast_ma) / slow_ma
                    confidence = min(0.95, min_confidence + ma_diff_pct)
                else:
                    signal_type = "HOLD"
                    confidence = 0.5
                
                # Create signal
                signal = Signal(
                    symbol=symbol,
                    signal_type=signal_type,
                    timestamp=datetime.now(),
                    confidence=confidence,
                    price=current_price,
                    additional_data={
                        "fast_ma": fast_ma,
                        "slow_ma": slow_ma,
                        "ma_crossover_pct": ((fast_ma - slow_ma) / slow_ma * 100),
                    }
                )
                
                signals.append(signal)
            
            except (ValueError, KeyError) as e:
                # Skip symbol if calculation fails
                continue
        
        # Cache signals for later reference
        self.cache_signals(signals)
        
        return signals
