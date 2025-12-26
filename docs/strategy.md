# FINDMY – Strategy Guide

## Overview

A **strategy** in FINDMY generates trading signals based on market data and analysis. Strategies are the "brain" of the trading system, while execution is the "body" that acts on those signals.

**Key Design Principle**: Strategies are **stateless** and isolated from execution. They take market data as input and produce order intents as output.

---

## Strategy Interface

### Input
```python
class StrategyInput:
    market_data: MarketDataFrame      # OHLCV data + indicators
    portfolio_state: PortfolioState   # Current positions, cash
    decision_context: DecisionContext # Risk limits, constraints
```

### Output
```python
class StrategySignal:
    symbol: str
    side: str              # "BUY" or "SELL"
    qty: float
    price: float
    urgency: str           # "IMMEDIATE", "NEXT_BAR", "BEST_EFFORT"
    reason: str            # Why this trade is being made
```

### Contract
```python
def generate_signals(
    market_data: MarketDataFrame,
    portfolio_state: PortfolioState
) -> List[StrategySignal]:
    """
    Pure function: market_data + portfolio → order signals.
    
    Must be deterministic and stateless.
    """
```

---

## Strategy Principles

### 1. **Statelessness**
- No internal state between calls
- All context comes from inputs
- Same inputs → same outputs
- Enables parallelization and backtesting

**Bad** ❌:
```python
class BadStrategy:
    def __init__(self):
        self.position_count = 0  # State!
    
    def signal(self, data):
        self.position_count += 1  # Mutating state!
        return Order(...)
```

**Good** ✅:
```python
def good_strategy(market_data, portfolio_state):
    position_count = len(portfolio_state.positions)  # Input!
    if position_count < max_positions:
        return signals
```

### 2. **No Look-Ahead Bias**
- Only use data available at decision time
- No future data (from tomorrow, next bar, etc.)
- Respect bar/candle boundaries

**Bad** ❌:
```python
def biased_signal(historical_data):
    future_price = historical_data.iloc[i + 5]['close']  # Look-ahead!
    if future_price > current_price:
        return BUY_SIGNAL
```

**Good** ✅:
```python
def unbiased_signal(current_bar, previous_bars):
    # Only use current_bar and previous_bars
    # Never peek at future bars
    momentum = current_bar['close'] - previous_bars[-1]['close']
    if momentum > 0:
        return BUY_SIGNAL
```

### 3. **Explicit Reasoning**
- Every signal must have a documented reason
- Enables post-trade analysis and debugging
- Helps identify signal failures

**Example**:
```python
signal = StrategySignal(
    symbol="BTC/USDT",
    side="BUY",
    qty=0.5,
    price=65000,
    reason="RSI < 30 and price above SMA(200); mean-reversion play"
)
```

### 4. **Determinism**
- No randomness (`random.random()`, `np.random.choice()`, etc.)
- No external API calls
- No current time dependencies

**Bad** ❌:
```python
def random_signal(data):
    if random.random() > 0.5:  # Non-deterministic!
        return BUY_SIGNAL
```

**Good** ✅:
```python
def deterministic_signal(data):
    if data['rsi'][-1] < 30:  # Deterministic!
        return BUY_SIGNAL
```

### 5. **Single Responsibility**
- Generate signals only
- Don't execute trades
- Don't manage positions
- Don't calculate P&L

---

## Example Strategies

### Example 1: Simple Mean Reversion

```python
import pandas as pd
from findmy.execution.paper_execution import Order

class MeanReversionStrategy:
    """
    Buy when price is 2 std devs below 20-day moving average.
    Sell when price returns to the average.
    """
    
    def __init__(self, lookback=20, std_dev_threshold=2.0):
        self.lookback = lookback
        self.std_dev_threshold = std_dev_threshold
    
    def generate_signals(self, market_data, portfolio_state):
        """
        Args:
            market_data: DataFrame with columns ['close']
            portfolio_state: Current positions
            
        Returns:
            List of trading signals
        """
        signals = []
        
        # Calculate mean and std dev
        closes = market_data['close'].tail(self.lookback)
        mean = closes.mean()
        std = closes.std()
        lower_band = mean - (self.std_dev_threshold * std)
        
        current_price = market_data['close'].iloc[-1]
        
        # Signal: Buy if below band
        if current_price < lower_band:
            qty = self._calculate_quantity(portfolio_state)
            signals.append(Order(
                symbol=market_data.attrs['symbol'],
                side='BUY',
                qty=qty,
                price=current_price,
                reason=f"Price {current_price:.0f} < lower band {lower_band:.0f}"
            ))
        
        return signals
    
    def _calculate_quantity(self, portfolio_state):
        # Use 10% of available capital
        return portfolio_state.available_cash / (portfolio_state.current_price * 10)
```

### Example 2: Momentum + Mean Reversion Hybrid

```python
class HybridStrategy:
    """
    Long-term momentum + short-term mean reversion:
    - Buy when price is above 200-day SMA (momentum)
    - AND RSI < 30 (mean reversion dip)
    - Sell when RSI > 70 (overbought)
    """
    
    def generate_signals(self, market_data, portfolio_state):
        signals = []
        closes = market_data['close']
        
        # Long-term trend
        sma_200 = closes.rolling(200).mean().iloc[-1]
        current_price = closes.iloc[-1]
        
        # Short-term momentum
        rsi = self._calculate_rsi(closes)
        
        # Entry: momentum + oversold
        if current_price > sma_200 and rsi < 30:
            signals.append(Order(
                symbol=market_data.attrs['symbol'],
                side='BUY',
                qty=0.1,
                price=current_price,
                reason=f"Above 200SMA ({sma_200:.0f}) + RSI oversold ({rsi:.1f})"
            ))
        
        # Exit: overbought
        if rsi > 70 and portfolio_state.has_position(market_data.attrs['symbol']):
            signals.append(Order(
                symbol=market_data.attrs['symbol'],
                side='SELL',
                qty=portfolio_state.position_size,
                price=current_price,
                reason=f"RSI overbought ({rsi:.1f})"
            ))
        
        return signals
    
    def _calculate_rsi(self, closes, period=14):
        delta = closes.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1]
```

### Example 3: Dollar-Cost Averaging (Passive)

```python
class DCAStrategy:
    """
    Simple dollar-cost averaging:
    - Buy fixed amount every period
    - No signal logic, mechanical execution
    """
    
    def __init__(self, investment_per_buy=1000, buy_frequency_days=7):
        self.investment_per_buy = investment_per_buy
        self.buy_frequency_days = buy_frequency_days
    
    def generate_signals(self, market_data, portfolio_state):
        # Check if enough time has passed since last buy
        last_buy = portfolio_state.last_buy_time
        days_since_buy = (portfolio_state.current_date - last_buy).days
        
        if days_since_buy >= self.buy_frequency_days:
            current_price = market_data['close'].iloc[-1]
            qty = self.investment_per_buy / current_price
            
            return [Order(
                symbol=market_data.attrs['symbol'],
                side='BUY',
                qty=qty,
                price=current_price,
                reason=f"DCA: periodic buy (${self.investment_per_buy})"
            )]
        
        return []
```

---

## Testing Strategies

### Unit Test Example

```python
import unittest
from pandas import DataFrame

class TestMeanReversionStrategy(unittest.TestCase):
    
    def setUp(self):
        self.strategy = MeanReversionStrategy(lookback=20, std_dev_threshold=2.0)
    
    def test_buy_signal_when_oversold(self):
        # Create mock data: 20 days, mean = 100, std = 5
        # Current price = 85 (2.5 std deviations below mean)
        data = DataFrame({
            'close': [100] * 19 + [85]
        })
        data.attrs['symbol'] = 'BTC/USDT'
        
        portfolio = PortfolioState(available_cash=1000, positions={})
        
        signals = self.strategy.generate_signals(data, portfolio)
        
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, 'BUY')
        self.assertEqual(signals[0].symbol, 'BTC/USDT')
    
    def test_no_signal_when_normal(self):
        # All prices around 100, price = 100
        data = DataFrame({
            'close': [100 + i*0.1 for i in range(20)]
        })
        data.attrs['symbol'] = 'BTC/USDT'
        
        portfolio = PortfolioState(available_cash=1000, positions={})
        
        signals = self.strategy.generate_signals(data, portfolio)
        
        self.assertEqual(len(signals), 0)
```

---

## Best Practices

1. **Start simple**: Begin with basic rules (e.g., SMA crossover)
2. **Backtest first**: Test on historical data before paper trading
3. **Document assumptions**: Clearly state market conditions, assumptions
4. **Limit parameters**: Fewer parameters = less overfitting
5. **Monitor performance**: Track win rate, average return, drawdown
6. **Avoid over-optimization**: Parameter-fit strategies often fail live
7. **Size positions conservatively**: Start with small position sizes
8. **Have an exit plan**: Define stop-loss and profit targets upfront

---

## Common Mistakes to Avoid

| Mistake | Problem | Solution |
|---------|---------|----------|
| Look-ahead bias | Signals future information | Use only current/past bars |
| Parameter overfitting | Works on backtest, fails live | Use cross-validation, simple params |
| Ignoring transaction costs | Paper P&L inflated | Include slippage, fees in backtest |
| No risk management | Losses too large | Set position size limits, stops |
| Emotional trading | Deviating from rules | Automate signal execution |
| No documentation | Can't debug failures | Comment reasoning behind signals |

---

## Integration with FINDMY

### Workflow

```
1. Strategy generates signals
   ↓
2. Signals → Order intents
   ↓
3. Risk checks (pre-trade validation)
   ↓
4. Execution engine processes orders
   ↓
5. Results persisted to SOT
   ↓
6. Analysis & reporting
```

### Example Integration

```python
from findmy.execution.paper_execution import PaperExecutionEngine
from my_strategy import MeanReversionStrategy

# Initialize
strategy = MeanReversionStrategy()
engine = PaperExecutionEngine()

# Load market data
market_data = load_market_data("BTC/USDT")
portfolio = load_portfolio_state()

# Generate signals
signals = strategy.generate_signals(market_data, portfolio)

# Execute
result = engine.execute(signals)

# Persist to SOT
save_to_sot(result)
```

---

## References

- **Architecture**: See [architecture.md](architecture.md)
- **Execution**: See [execution.md](execution.md)
- **Backtesting**: See [roadmap.md](roadmap.md)
