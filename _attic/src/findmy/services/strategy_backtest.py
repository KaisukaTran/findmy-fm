"""Strategy backtesting service with performance metrics."""

from typing import List, Dict, Any, Optional
from datetime import datetime
import logging

from findmy.strategies.base import Strategy
from findmy.services.market_data import get_historical_range

logger = logging.getLogger(__name__)


class StrategyBacktestResult:
    """Results from strategy backtest execution."""
    
    def __init__(self):
        """Initialize backtest result container."""
        self.strategy_name: str = ""
        self.symbols: List[str] = []
        self.start_date: datetime = None
        self.end_date: datetime = None
        self.initial_capital: float = 0.0
        self.final_equity: float = 0.0
        self.equity_curve: List[Dict[str, Any]] = []
        self.trades: List[Dict[str, Any]] = []
        self.signals: List[Dict[str, Any]] = []
        self.metrics: Dict[str, float] = {}
        self.status: str = "completed"
        self.error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON response."""
        return {
            "strategy_name": self.strategy_name,
            "symbols": self.symbols,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "initial_capital": self.initial_capital,
            "final_equity": self.final_equity,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
            "signals": self.signals,
            "metrics": self.metrics,
            "status": self.status,
            "error": self.error,
        }


class StrategyBacktester:
    """Backtests trading strategies over historical data with performance tracking."""
    
    def __init__(self, strategy: Strategy):
        """
        Initialize backtester with strategy.
        
        Args:
            strategy: Strategy instance to backtest
        """
        self.strategy = strategy
    
    def run(
        self,
        start_date: datetime,
        end_date: datetime,
        initial_capital: float = 10000.0,
        timeframe: str = "1h"
    ) -> StrategyBacktestResult:
        """
        Run strategy backtest over historical data.
        
        Args:
            start_date: Backtest start date
            end_date: Backtest end date
            initial_capital: Starting capital in USD
            timeframe: OHLCV timeframe (e.g., "1h", "4h", "1d")
        
        Returns:
            StrategyBacktestResult with performance metrics
        """
        result = StrategyBacktestResult()
        result.strategy_name = self.strategy.name
        result.symbols = self.strategy.symbols
        result.start_date = start_date
        result.end_date = end_date
        result.initial_capital = initial_capital
        result.final_equity = initial_capital
        
        try:
            # Fetch market data for all symbols
            market_data = {}
            for symbol in self.strategy.symbols:
                try:
                    candles = get_historical_range(
                        symbol=symbol,
                        start_datetime=start_date,
                        end_datetime=end_date,
                        timeframe=timeframe
                    )
                    if candles:
                        market_data[symbol] = candles
                except Exception as e:
                    logger.warning(f"Failed to fetch data for {symbol}: {e}")
                    continue
            
            if not market_data:
                result.status = "error"
                result.error = "No market data available for any symbols"
                return result
            
            # Initialize tracking variables
            positions = {}  # {symbol: {"qty": float, "entry_price": float, "entry_time": datetime}}
            cash = initial_capital
            equity_curve = []
            all_signals = []
            all_trades = []
            
            # Find the minimum number of candles across all symbols
            min_candles = min(len(candles) for candles in market_data.values())
            
            # Iterate through each time step
            for i in range(min_candles):
                # Get current candle data for all symbols
                current_prices = {}
                current_time = None
                
                for symbol, candles in market_data.items():
                    candle = candles[i]
                    current_prices[symbol] = candle["close"]
                    current_time = datetime.fromtimestamp(candle["timestamp"] / 1000)
                
                # Get historical data up to current point for strategy
                historical_data = {}
                for symbol, candles in market_data.items():
                    historical_data[symbol] = candles[:i+1]
                
                # Generate signals from strategy
                try:
                    signals = self.strategy.generate_signals(historical_data, current_prices)
                except Exception as e:
                    logger.warning(f"Signal generation failed at {current_time}: {e}")
                    signals = []
                
                # Process signals
                for signal in signals:
                    if signal.signal_type == "HOLD":
                        continue
                    
                    all_signals.append({
                        "timestamp": current_time.isoformat(),
                        "symbol": signal.symbol,
                        "signal_type": signal.signal_type,
                        "price": current_prices.get(signal.symbol, 0),
                        "confidence": signal.confidence,
                    })
                    
                    # Execute trade based on signal
                    try:
                        trade = self._execute_signal(
                            signal,
                            current_prices,
                            positions,
                            cash,
                            current_time
                        )
                        if trade:
                            all_trades.append(trade)
                            if signal.signal_type == "BUY":
                                cash -= trade["total_cost"]
                            else:  # SELL
                                cash += trade["proceeds"]
                    except Exception as e:
                        logger.warning(f"Trade execution failed: {e}")
                        continue
                
                # Update positions with current prices for mark-to-market
                position_value = sum(
                    pos["qty"] * current_prices.get(symbol, 0)
                    for symbol, pos in positions.items()
                )
                
                # Calculate current equity
                current_equity = cash + position_value
                
                # Record equity curve point
                equity_curve.append({
                    "timestamp": current_time.isoformat(),
                    "equity": current_equity,
                    "cash": cash,
                    "position_value": position_value,
                })
            
            # Calculate performance metrics
            result.final_equity = equity_curve[-1]["equity"] if equity_curve else initial_capital
            result.equity_curve = equity_curve
            result.signals = all_signals
            result.trades = all_trades
            result.metrics = self._calculate_metrics(
                initial_capital,
                result.final_equity,
                equity_curve,
                all_trades
            )
            
        except Exception as e:
            logger.error(f"Backtest failed: {e}")
            result.status = "error"
            result.error = str(e)
        
        return result
    
    def _execute_signal(
        self,
        signal,
        current_prices: Dict[str, float],
        positions: Dict[str, Dict[str, Any]],
        cash: float,
        timestamp: datetime
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a trade based on a signal.
        
        Args:
            signal: Trading signal
            current_prices: Current prices for all symbols
            positions: Current positions
            cash: Available cash
            timestamp: Trade timestamp
        
        Returns:
            Trade dictionary with execution details or None
        """
        symbol = signal.symbol
        price = current_prices.get(symbol, 0)
        
        if signal.signal_type == "BUY":
            # Calculate order size based on confidence
            # Use a fraction of available cash
            available_for_trade = cash * 0.1  # Use 10% of cash per trade
            quantity = available_for_trade / price if price > 0 else 0
            
            if quantity <= 0 or cash < available_for_trade:
                return None
            
            total_cost = quantity * price
            
            # Update or create position
            if symbol in positions:
                # Average up
                old_pos = positions[symbol]
                new_qty = old_pos["qty"] + quantity
                new_avg_price = (
                    (old_pos["qty"] * old_pos["entry_price"]) + (quantity * price)
                ) / new_qty
                positions[symbol] = {
                    "qty": new_qty,
                    "entry_price": new_avg_price,
                    "entry_time": old_pos["entry_time"],
                }
            else:
                positions[symbol] = {
                    "qty": quantity,
                    "entry_price": price,
                    "entry_time": timestamp,
                }
            
            return {
                "timestamp": timestamp.isoformat(),
                "symbol": symbol,
                "side": "BUY",
                "quantity": round(quantity, 8),
                "price": price,
                "total_cost": round(total_cost, 2),
            }
        
        elif signal.signal_type == "SELL":
            # Sell available position
            if symbol not in positions:
                return None
            
            position = positions[symbol]
            quantity = position["qty"]
            proceeds = quantity * price
            pnl = proceeds - (quantity * position["entry_price"])
            
            # Remove position
            del positions[symbol]
            
            return {
                "timestamp": timestamp.isoformat(),
                "symbol": symbol,
                "side": "SELL",
                "quantity": round(quantity, 8),
                "price": price,
                "proceeds": round(proceeds, 2),
                "pnl": round(pnl, 2),
            }
        
        return None
    
    def _calculate_metrics(
        self,
        initial_capital: float,
        final_equity: float,
        equity_curve: List[Dict[str, Any]],
        trades: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """
        Calculate performance metrics.
        
        Args:
            initial_capital: Starting capital
            final_equity: Ending equity
            equity_curve: List of equity values over time
            trades: List of executed trades
        
        Returns:
            Dictionary of performance metrics
        """
        metrics = {}
        
        # Basic returns
        total_return = final_equity - initial_capital
        total_return_pct = (total_return / initial_capital * 100) if initial_capital > 0 else 0
        
        metrics["total_return"] = round(total_return, 2)
        metrics["total_return_pct"] = round(total_return_pct, 2)
        
        # Max drawdown
        if equity_curve:
            peak = max(eq["equity"] for eq in equity_curve)
            trough = min(eq["equity"] for eq in equity_curve)
            max_drawdown = peak - trough
            max_drawdown_pct = (max_drawdown / peak * 100) if peak > 0 else 0
            metrics["max_drawdown"] = round(max_drawdown, 2)
            metrics["max_drawdown_pct"] = round(max_drawdown_pct, 2)
        
        # Trade metrics
        if trades:
            buy_trades = [t for t in trades if t["side"] == "BUY"]
            sell_trades = [t for t in trades if t["side"] == "SELL"]
            
            metrics["buy_count"] = len(buy_trades)
            metrics["sell_count"] = len(sell_trades)
            metrics["total_trades"] = len(trades)
            
            # Win rate (for completed round trips)
            winning_trades = [t for t in sell_trades if t.get("pnl", 0) > 0]
            win_rate = (len(winning_trades) / len(sell_trades) * 100) if sell_trades else 0
            metrics["win_rate_pct"] = round(win_rate, 2)
            
            # Total realized P&L
            total_realized_pnl = sum(t.get("pnl", 0) for t in sell_trades)
            metrics["realized_pnl"] = round(total_realized_pnl, 2)
        else:
            metrics["buy_count"] = 0
            metrics["sell_count"] = 0
            metrics["total_trades"] = 0
            metrics["win_rate_pct"] = 0.0
            metrics["realized_pnl"] = 0.0
        
        return metrics
