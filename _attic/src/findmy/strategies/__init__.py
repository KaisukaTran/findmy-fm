"""Trading strategies module for FINDMY.

This module provides a framework for implementing trading strategies,
including base classes and example implementations.

Example usage:
    from findmy.strategies import MovingAverageStrategy
    
    # Create strategy instance
    strategy = MovingAverageStrategy(
        symbols=["BTC", "ETH"],
        config={"fast_period": 9, "slow_period": 21}
    )
    
    # Generate signals
    signals = strategy.generate_signals(market_data, current_prices)
    
    # Process signals (BUY/SELL/HOLD)
    for signal in signals:
        print(f"{signal.symbol}: {signal.signal_type} @ {signal.price}")
"""

from .base import Strategy, Signal
from .moving_average import MovingAverageStrategy

__all__ = [
    "Strategy",
    "Signal",
    "MovingAverageStrategy",
]
