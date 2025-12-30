"""
Market Integration & Backtesting (v0.4.0)

This document covers the realtime market data integration and basic backtesting
features introduced in v0.4.0 of FINDMY FM.

Table of Contents:
1. Overview
2. Realtime Market Data
3. Unrealized PnL Calculation
4. WebSocket Live Updates
5. Backtesting Service
6. API Endpoints
7. Examples
8. Troubleshooting
"""

# 1. OVERVIEW
===============================================================

v0.4.0 introduces realtime market data integration and backtesting capabilities:

- **Binance Public API Integration**: Fetch realtime and historical prices for all major trading pairs
- **Realtime Unrealized PnL**: Dashboard shows live portfolio valuation with unrealized gains/losses
- **WebSocket Live Updates**: Dashboard updates every 30 seconds without page reload
- **Historical Data Fetching**: OHLCV data for backtesting and analysis
- **Basic Backtesting**: Simulate trading strategies over historical periods
- **Performance Metrics**: Equity curves, returns, drawdowns, Sharpe ratios


# 2. REALTIME MARKET DATA
===============================================================

The market data service fetches prices from Binance using the CCXT library.

## Price Caching

Prices are cached in memory with a 60-second TTL to avoid rate limiting:

```python
from findmy.services.market_data import get_current_prices, clear_cache

# Fetch prices for multiple symbols
prices = get_current_prices(["BTC", "ETH"])
# Returns: {"BTC": 45000.50, "ETH": 2500.25}

# Clear cache if needed
clear_cache()
```

## Historical Data

Fetch OHLCV data for backtesting:

```python
from findmy.services.market_data import get_historical_ohlcv, get_historical_range
from datetime import datetime, timedelta

# Last 100 1-hour candles
ohlcv = get_historical_ohlcv("BTC", timeframe="1h", limit=100)
# Returns: [{"timestamp": ms, "open": float, "high": float, "low": float, "close": float, "volume": float}, ...]

# Date range query
start = datetime(2024, 1, 1)
end = datetime(2024, 1, 31)
ohlcv = get_historical_range("ETH", start, end, timeframe="1d")
```

## Supported Timeframes

- "1m" - 1 minute
- "5m" - 5 minutes  
- "15m" - 15 minutes
- "30m" - 30 minutes
- "1h" - 1 hour (default)
- "4h" - 4 hours
- "1d" - 1 day


# 3. UNREALIZED PnL CALCULATION
===============================================================

Unrealized PnL is calculated automatically for all open positions.

## Formula

```
Current Price = Latest Binance ticker price
Market Value = Quantity × Current Price
Unrealized PnL = Market Value - Cost Basis (quantity × avg_entry_price)
Unrealized % = (Unrealized PnL / Cost Basis) × 100
```

## API Fields

All position endpoints return:

```json
{
  "symbol": "BTC",
  "quantity": 0.5,
  "avg_price": 45000.00,
  "total_cost": 22500.00,
  "current_price": 47000.00,
  "market_value": 23500.00,
  "unrealized_pnl": 1000.00
}
```

## Offline Fallback

If Binance is unavailable, the system:
1. Returns last cached prices (60-second TTL)
2. Displays null for current_price and unrealized_pnl
3. Shows dashboard with last known values
4. Retries on next update


# 4. WEBSOCKET LIVE UPDATES
===============================================================

The dashboard uses WebSocket for realtime updates without page reload.

## Connection

The WebSocket endpoint `/ws/dashboard` broadcasts updates every 30 seconds:

```javascript
// Automatic in dashboard.html
const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const ws = new WebSocket(`${protocol}//${window.location.host}/ws/dashboard`);

ws.onmessage = function(event) {
    const data = JSON.parse(event.data);
    // Update positions and summary with fresh data
};
```

## Update Message Format

```json
{
  "type": "dashboard_update",
  "timestamp": "2024-12-30T12:00:00",
  "positions": [
    {
      "symbol": "BTC",
      "quantity": 0.5,
      "avg_price": 45000.00,
      "total_cost": 22500.00,
      "current_price": 47000.00,
      "market_value": 23500.00,
      "unrealized_pnl": 1000.00
    }
  ],
  "summary": {
    "total_trades": 42,
    "realized_pnl": 2500.00,
    "unrealized_pnl": 1000.00,
    "total_invested": 50000.00,
    "total_market_value": 51000.00,
    "total_equity": 52500.00
  }
}
```

## Fallback Behavior

If WebSocket is unavailable (firewall, reverse proxy):
1. Dashboard falls back to polling every 60 seconds
2. Updates still work, just less frequently
3. Automatic reconnection attempts every 5 seconds


# 5. BACKTESTING SERVICE
===============================================================

Simulate trading strategies over historical data to test performance.

## Basic Usage

```python
from findmy.services.backtesting import run_backtest, BacktestRequest
from datetime import datetime, timedelta

request = BacktestRequest(
    symbols=["BTC", "ETH"],
    start_date=datetime(2024, 1, 1),
    end_date=datetime(2024, 3, 31),
    initial_capital=10000.0,
    timeframe="1h"
)

result = run_backtest(request)

# Result contains:
# - equity_curve: List of equity values over time
# - trades: List of executed trades
# - metrics: Performance statistics
# - status: "completed" or "error"
# - error: Error message if status == "error"
```

## Result Structure

```python
{
    "equity_curve": [
        {
            "timestamp": 1704067200000,
            "timestamp_dt": "2024-01-01T00:00:00",
            "equity": 10000.00,
            "cash": 10000.00
        }
    ],
    "trades": [
        {
            "symbol": "BTC",
            "side": "BUY",
            "quantity": 0.1,
            "price": 42000.00,
            "pnl": 500.00
        }
    ],
    "metrics": {
        "initial_capital": 10000.00,
        "final_equity": 11500.00,
        "total_return_pct": 15.0,
        "max_drawdown_pct": 5.2,
        "total_trades": 8,
        "winning_trades": 6,
        "losing_trades": 2,
        "win_rate_pct": 75.0,
        "sharpe_ratio": 1.8,
        "backtest_period": "2024-01-01 to 2024-03-31"
    },
    "status": "completed",
    "error": null
}
```

## Performance Metrics

- **total_return_pct**: Total return on initial capital (%)
- **max_drawdown_pct**: Maximum peak-to-trough decline (%)
- **win_rate_pct**: Percentage of winning trades (%)
- **sharpe_ratio**: Risk-adjusted return (higher is better)

## API Endpoint

POST /api/backtest

```json
{
  "symbols": ["BTC", "ETH"],
  "start_date": "2024-01-01",
  "end_date": "2024-03-31",
  "initial_capital": 10000.0,
  "timeframe": "1h"
}
```


# 6. API ENDPOINTS
===============================================================

## Health Check

GET /health

```json
{
  "status": "ok",
  "service": "FINDMY FM API"
}
```

## Positions with Unrealized PnL

GET /api/positions

Returns current positions with realtime prices and unrealized PnL:

```json
[
  {
    "symbol": "BTC",
    "quantity": 0.5,
    "avg_price": 45000.00,
    "total_cost": 22500.00,
    "current_price": 47000.00,
    "market_value": 23500.00,
    "unrealized_pnl": 1000.00
  }
]
```

## Summary with Market Values

GET /api/summary

Returns portfolio summary with realtime market valuation:

```json
{
  "total_trades": 42,
  "realized_pnl": 2500.00,
  "unrealized_pnl": 1000.00,
  "total_invested": 50000.00,
  "total_market_value": 51000.00,
  "total_equity": 52500.00,
  "last_trade_time": "2024-12-30T10:30:00",
  "status": "✓ Active"
}
```

## Trade History

GET /api/trades

Returns all trades with realized PnL:

```json
[
  {
    "id": 1,
    "symbol": "BTC",
    "side": "BUY",
    "entry_qty": 0.5,
    "entry_price": 45000.00,
    "entry_time": "2024-12-20T10:00:00",
    "exit_qty": 0.5,
    "exit_price": 47000.00,
    "exit_time": "2024-12-25T15:30:00",
    "status": "CLOSED",
    "realized_pnl": 1000.00
  }
]
```

## WebSocket Dashboard

WS /ws/dashboard

Realtime updates every 30 seconds with positions and summary. Auto-reconnects on disconnect.

## Backtesting

POST /api/backtest

Run backtest simulation. See section 5 for details.

```bash
curl -X POST http://localhost:8000/api/backtest \
  -H "Content-Type: application/json" \
  -d '{
    "symbols": ["BTC"],
    "start_date": "2024-01-01",
    "end_date": "2024-03-31",
    "initial_capital": 10000.0,
    "timeframe": "1h"
  }'
```


# 7. EXAMPLES
===============================================================

## Example 1: Monitor Portfolio Unrealized PnL

```python
import asyncio
import httpx

async def monitor_portfolio():
    async with httpx.AsyncClient() as client:
        # Get positions with current prices
        response = await client.get("http://localhost:8000/api/positions")
        positions = response.json()
        
        for pos in positions:
            print(f"{pos['symbol']}: {pos['unrealized_pnl']:+.2f} ({pos['quantity']} @ {pos['current_price']})")
        
        # Get summary
        summary_resp = await client.get("http://localhost:8000/api/summary")
        summary = summary_resp.json()
        print(f"Total Portfolio Value: ${summary['total_market_value']:.2f}")
        print(f"Unrealized PnL: ${summary['unrealized_pnl']:+.2f}")

asyncio.run(monitor_portfolio())
```

## Example 2: Backtest a Strategy

```python
import requests
from datetime import datetime, timedelta

# Backtest last 90 days
end_date = datetime.now()
start_date = end_date - timedelta(days=90)

response = requests.post("http://localhost:8000/api/backtest", json={
    "symbols": ["BTC", "ETH"],
    "start_date": start_date.strftime("%Y-%m-%d"),
    "end_date": end_date.strftime("%Y-%m-%d"),
    "initial_capital": 50000,
    "timeframe": "4h"
})

result = response.json()
print(f"Final Equity: ${result['metrics']['final_equity']:.2f}")
print(f"Return: {result['metrics']['total_return_pct']:.1f}%")
print(f"Max Drawdown: {result['metrics']['max_drawdown_pct']:.1f}%")
print(f"Sharpe Ratio: {result['metrics']['sharpe_ratio']:.2f}")
```

## Example 3: WebSocket Real-time Monitor

```javascript
// JavaScript in browser
const ws = new WebSocket('ws://localhost:8000/ws/dashboard');

ws.onmessage = function(event) {
    const data = JSON.parse(event.data);
    console.log(`Updated at ${data.timestamp}`);
    console.log(`Total Equity: $${data.summary.total_equity}`);
    
    data.positions.forEach(pos => {
        console.log(`${pos.symbol}: ${pos.unrealized_pnl > 0 ? '+' : ''}${pos.unrealized_pnl.toFixed(2)}`);
    });
};

ws.onerror = function(error) {
    console.error('WebSocket error:', error);
};
```


# 8. TROUBLESHOOTING
===============================================================

## Issue: current_price shows null

**Cause**: Binance API is unreachable or rate limited

**Solutions**:
1. Check internet connection
2. Verify Binance API status (https://status.binance.com)
3. Wait 60+ seconds for cache to expire and retry
4. Check for firewall/proxy blocking

## Issue: WebSocket doesn't connect

**Cause**: Firewall, reverse proxy, or browser security policy

**Solutions**:
1. Check browser console for CORS errors
2. Verify WebSocket support (check firewall rules)
3. Dashboard will fall back to polling every 60 seconds
4. Try from different network/device

## Issue: Backtest returns empty equity_curve

**Cause**: No historical data available for date range

**Solutions**:
1. Verify symbols are correct (e.g., "BTC" not "bitcoin")
2. Check that start_date < end_date
3. Ensure date range is within Binance historical availability (typically 1 year)
4. Try shorter timeframe (smaller data request)

## Issue: WebSocket loses connection

**Cause**: Normal - connections drop after ~60-90 seconds of inactivity

**Solutions**:
1. Dashboard automatically reconnects every 5 seconds
2. Updates continue via fallback polling
3. Manual refresh button available on dashboard

## Performance Considerations

- **Price fetching**: 1-2ms per symbol (cached)
- **WebSocket update**: 30s interval = 2 updates/minute
- **Backtest**: Depends on data size:
  - 1-week backtest: <500ms
  - 3-month backtest: 2-5s
  - 1-year backtest: 10-30s
