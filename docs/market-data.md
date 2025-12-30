# Market Data Integration

## Overview

The FINDMY FM dashboard now integrates real-time market data from **Binance public API** to enable mark-to-market valuation and unrealized PnL calculations. This allows traders to see live portfolio values and paper trading performance against actual market prices.

## Architecture

### Components

1. **Market Data Service** (`src/findmy/services/market_data.py`)
   - Fetches current prices from Binance for trading positions
   - Implements in-memory TTL cache to prevent rate limiting
   - Handles errors gracefully, returning last known prices or empty dict
   - Public API only – **no API key required**

2. **API Endpoints**
   - `GET /api/positions` – Returns positions with current prices and unrealized PnL
   - `GET /api/summary` – Returns portfolio summary with total market value and equity
   - `GET /api/trades` – Returns trade history (unchanged)

3. **Dashboard**
   - Real-time display of position market values and unrealized P&L
   - Colored indicators: Green (profit), Red (loss)
   - Auto-refreshes every 30 seconds

## Features

### Price Fetching

```python
from findmy.services.market_data import get_current_prices

symbols = ["BTC", "ETH", "SOL"]
prices = get_current_prices(symbols)
# Returns: {"BTC": 65000.0, "ETH": 3200.0, "SOL": 150.5}
```

### Unrealized PnL Calculation

```python
from findmy.services.market_data import get_unrealized_pnl

# For a position
unrealized_pnl, market_value = get_unrealized_pnl(
    symbol="BTC",
    quantity=0.5,
    avg_price=60000.0,
    current_price=65000.0
)
# unrealized_pnl = 2500.0 (profit)
# market_value = 32500.0
```

### Caching Strategy

- **Cache TTL**: 60 seconds
- **Implementation**: In-memory dictionary with timestamp tracking
- **Benefit**: Avoids rate limits while providing fresh data every minute
- **Fallback**: Returns last known prices if Binance is unavailable

## Symbol Mapping

The system assumes **base currency symbols** (e.g., "BTC", "ETH", "SOL") and automatically constructs Binance pairs as:
- `BTC` → `BTC/USDT`
- `ETH` → `ETH/USDT`
- `SOL` → `SOL/USDT`

All pairs are traded against USDT (Tether).

## Rate Limits

Binance public API has generous rate limits:
- **Spot prices**: 1200 requests/minute per IP
- **Cache TTL (60s)**: Effectively reduces queries to 1 per symbol per minute

**With cache, the system stays well within limits.**

## Error Handling

The system gracefully handles Binance outages or network issues:

1. **Individual symbol fails** → Skips that symbol, continues with others
2. **Entire Binance API fails** → Returns last cached prices
3. **No cache available** → Returns empty dict, position shows "—" in UI

Example:
```python
prices = get_current_prices(["BTC", "INVALID", "ETH"])
# Even if INVALID fails, returns {"BTC": ..., "ETH": ...}
```

## Dashboard Integration

### Positions Table

Displays for each position:
- **Symbol**: Asset name
- **Quantity**: Position size
- **Avg Price**: Entry price
- **Total Cost**: Cost basis (qty × avg_price)
- **Current Price**: Live market price from Binance *(new)*
- **Market Value**: Current position value (qty × current_price) *(new)*
- **Unrealized P&L**: Mark-to-market profit/loss *(new)*

### Summary Cards

- **Total Equity**: Cost basis + unrealized PnL
- **Total Market Value**: Sum of all position market values
- **Total Invested**: Sum of cost basis
- **Realized P&L**: From closed trades (unchanged)
- **Unrealized P&L**: From open positions (new)

### Price Source Badge

"Binance (live)" indicator shows data source. Plans for future data providers (Kraken, CoinGecko, etc.).

## API Response Examples

### GET /api/positions

```json
[
  {
    "symbol": "BTC",
    "quantity": 0.5,
    "avg_price": 60000.0,
    "total_cost": 30000.0,
    "current_price": 65000.0,
    "market_value": 32500.0,
    "unrealized_pnl": 2500.0
  },
  {
    "symbol": "ETH",
    "quantity": 10.0,
    "avg_price": 2500.0,
    "total_cost": 25000.0,
    "current_price": 3200.0,
    "market_value": 32000.0,
    "unrealized_pnl": 7000.0
  }
]
```

### GET /api/summary

```json
{
  "total_trades": 5,
  "realized_pnl": 1250.5,
  "unrealized_pnl": 9500.0,
  "total_invested": 55000.0,
  "total_market_value": 64500.0,
  "total_equity": 64500.0,
  "last_trade_time": "2025-01-10T14:30:00",
  "status": "✓ Active"
}
```

## Testing

The market data service includes comprehensive tests with mocked `ccxt`:

```bash
pytest tests/test_market_data.py -v
```

Tests cover:
- Price fetching for multiple symbols
- Cache TTL behavior
- Error handling and fallback
- Unrealized PnL calculations

## Future Enhancements

1. **Multiple price sources**: Add Kraken, CoinGecko, CoinMarketCap
2. **Price history**: Store hourly/daily OHLCV data for backtesting
3. **Alerts**: Notify when unrealized PnL exceeds thresholds
4. **Configuration**: Allow users to select preferred price source
5. **WebSocket prices**: Real-time streaming from Binance WebSocket API

## Troubleshooting

### Prices showing as "—"

**Cause**: Binance API unavailable or symbol not supported
**Solution**: Check Binance status, verify symbol format (e.g., "BTC" not "bitcoin")

### Cache not updating

**Cause**: TTL not expired yet
**Solution**: Wait 60 seconds or refresh manually (does not clear server cache)

### Rate limit errors

**Cause**: More than 1200 API calls/minute from your IP
**Solution**: Cache is working – this should not happen. Contact support if it does.

## References

- [CCXT Documentation](https://docs.ccxt.com/)
- [Binance API Docs](https://binance-docs.github.io/apidocs/)
- [Rate Limits](https://binance-docs.github.io/apidocs/#limits)
