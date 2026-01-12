# FINDMY FM Dashboard

## Overview

The **FINDMY FM Dashboard** is a beautiful, responsive HTML interface that provides real-time monitoring of the Trade Service (TS) and Statement of Truth (SOT) systems with live market data. It features mark-to-market valuation using real-time Binance prices and replaces manual Swagger UI interaction with an intuitive visual experience.

## Features

### � Tab-Based Navigation (v0.11.0)
The dashboard uses Bootstrap nav-tabs for organized content:
- **Overview**: System status, summary cards, risk metrics
- **Positions/Trades**: Current positions table, trade history
- **Pending Orders**: Order approval queue with actions
- **KSS Pyramid**: Dedicated tab for KSS pyramid DCA trading

### 📊 System Status
- **Database Connection**: Real-time verification of database connectivity
- **Trade Service Status**: Active/inactive state of the TS engine
- **SOT Audit Ready**: Verification that the Statement of Truth is audit-ready
- **Last Update Time**: Timestamp of the most recent trade execution

### 💼 Current Positions (with Live Prices)
A responsive table displaying all open positions with real-time market valuation:
- **Symbol**: Trading pair (e.g., BTC)
- **Quantity**: Current position size
- **Average Price**: Entry price or weighted average
- **Total Cost**: Total capital invested (qty × avg_price)
- **Current Price**: Live market price from Binance *(NEW)*
- **Market Value**: Current position value at market price (qty × current_price) *(NEW)*
- **Unrealized P&L**: Mark-to-market profit/loss, color-coded green (profit) / red (loss) *(NEW)*

### 📈 Trade History
Complete record of all trades with real-time sorting:
- **Timestamp**: Entry time with date and time
- **Symbol**: Trading pair
- **Side**: BUY or SELL (color-coded badges)
- **Quantity**: Entry quantity
- **Price**: Entry execution price
- **Status**: OPEN, CLOSED, or PARTIAL (color-coded)
- **Realized P&L**: Profit/loss for closed trades (green/red)

### 💰 Summary Cards (Enhanced)
Key performance metrics at a glance:
- **Total Trades**: Cumulative trade count
- **Realized P&L**: Profits from closed positions (green if positive, red if negative)
- **Unrealized P&L**: Current open position P&L from mark-to-market valuation (green if positive, red if negative) *(NEW)*
- **Total Equity**: Cost basis + unrealized PnL (total portfolio value) *(NEW)*
- **Total Invested**: Total capital deployed across positions *(NEW)*
- **Market Value**: Sum of all position market values at current prices *(NEW)*
- **Price Source**: Binance public API (live, refreshed every 60 seconds) *(NEW)*

### 🔺 KSS Pyramid Tab (v0.11.0)
Dedicated tab for KSS pyramid DCA trading:
- **Session Summary**: Active sessions, total/used fund, unrealized PnL
- **Create Form**: Inline form with symbol, entry price, distance %, max waves, etc.
- **Preview**: Click to preview projected waves before creating
- **Sessions Table**: View all sessions with status, avg price, TP target, PnL
- **Realtime Waves Table**: View wave details for selected session
- **Chart.js Visualization**: Price levels chart with legend (Avg=yellow dashed, TP=green dashed, Market=blue solid)

## Real-Time Updates (v0.11.0)

### No Page Reload Architecture
The dashboard now uses **selective DOM updates** instead of full page refresh:
- WebSocket receives updates and updates only specific elements
- Form inputs are preserved during updates (no lost data)
- Loading spinner indicates background fetch activity

### WebSocket Selective Updates
```javascript
// WebSocket updates specific DOM elements
ws.onmessage = function(event) {
    const data = JSON.parse(event.data);
    if (data.positions) updatePositionsDOM(data.positions);
    if (data.summary) updateSummaryDOM(data.summary);
    if (data.kss_sessions) updateKSSSessionsDOM(data.kss_sessions);
};
```

### Fallback Polling
- **Every 30s**: Summary, positions, pending orders, KSS sessions
- **Every 60s**: Trades, risk metrics (less volatile data)

### localStorage Form Persistence
KSS form inputs are automatically saved to localStorage on change and restored on page load, preventing data loss during updates.

## Real-Time Market Data

### Live Price Integration

The dashboard uses **Binance public API** (via CCXT) to fetch real-time spot prices:

- **No API key required** – Public data only
- **Symbols**: Assumes base currency symbols (BTC, ETH, SOL) and creates USDT pairs automatically
- **Refresh rate**: 60-second cache TTL prevents rate limiting while keeping prices fresh
- **Fallback**: Shows last known prices if Binance is temporarily unavailable

### Mark-to-Market Valuation

Each position's unrealized P&L is calculated in real-time:

```
Market Value = Quantity × Current Price
Unrealized P&L = Market Value - Total Cost
Total Equity = Total Cost + Unrealized P&L
```

## Accessing the Dashboard

### Local Development
```bash
# Start the API server
uvicorn findmy.api.main:app --reload

# Open in browser
http://localhost:8000/
```

### Docker

```bash
docker run -p 8000:8000 findmy-fm
# Navigate to http://localhost:8000/
```

## Real-Time Updates

The dashboard uses selective DOM updates instead of page refresh:

1. **WebSocket**: Real-time updates from server push data to specific DOM elements
2. **Fallback Polling**: Every 30s for positions/pending, 60s for trades/risk
3. **No Page Reload**: Form inputs preserved, no interruption to user workflow
4. **Loading Spinner**: Visual feedback during background fetches

To adjust refresh intervals, edit `templates/dashboard.html` and modify the `setInterval` calls in the initialization section.

## API Endpoints

The dashboard consumes three REST API endpoints:

### GET /api/positions
Returns current open positions.

**Response:**
```json
[
  {
    "symbol": "BTC/USDT",
    "quantity": 0.5,
    "avg_price": 45000.0,
    "total_cost": 22500.0
  }
]
```

### GET /api/trades
Returns complete trade history (ordered by timestamp DESC).

**Response:**
```json
[
  {
    "id": 1,
    "symbol": "BTC/USDT",
    "side": "BUY",
    "entry_qty": 0.5,
    "entry_price": 45000.0,
    "entry_time": "2025-12-30T10:15:30",
    "exit_qty": null,
    "exit_price": null,
    "exit_time": null,
    "status": "OPEN",
    "realized_pnl": null
  }
]
```

### GET /api/summary
Returns aggregated summary statistics.

**Response:**
```json
{
  "total_trades": 42,
  "realized_pnl": 1234.56,
  "unrealized_pnl": 567.89,
  "total_invested": 100000.0,
  "last_trade_time": "2025-12-30T15:45:22",
  "status": "✓ Active"
}
```

## Styling & Customization

### Files
- **Base Template**: `templates/base.html` - Navbar, footer, Bootstrap framework
- **Dashboard Template**: `templates/dashboard.html` - Dashboard content and data loading logic
- **Styles**: `static/css/style.css` - Custom colors, gradients, responsive design

### Customization Examples

#### Change Color Scheme
Edit `static/css/style.css`:
```css
:root {
    --primary-color: #0d6efd;     /* Change to your brand color */
    --success-color: #198754;
    --danger-color: #dc3545;
    --warning-color: #ffc107;
    --info-color: #0dcaf0;
}
```

#### Add Dark Mode
The CSS includes a `@media (prefers-color-scheme: dark)` block for future dark mode support.

#### Modify Refresh Rate
In `templates/dashboard.html`, change the interval values:
```javascript
setInterval(() => { ... }, 10000);  // Change 10000 to desired milliseconds
```

## Future Enhancements

### Phase 3 Roadmap
- [ ] **Live Binance Integration**: Replace mock data with real market feeds
- [ ] **Advanced Charts**: TradingView or Chart.js candlestick charts
- [ ] **Alerts & Notifications**: Email/webhook alerts for trade milestones
- [ ] **Export Data**: CSV/PDF export of positions and trade history
- [ ] **Dark Mode Toggle**: User-facing dark/light mode switcher
- [ ] **Multi-Account Support**: Dashboard for multiple trading accounts
- [ ] **Risk Metrics**: Drawdown, Sharpe ratio, win rate analytics
- [ ] **Order Management**: Direct order placement from dashboard

### Integration Points
- Market data from Binance API
- WebSocket support for tick-by-tick updates
- Historical data archival and analytics
- Machine learning model performance tracking

## Troubleshooting

### Dashboard shows "Loading..." forever
- Check browser console (F12) for JavaScript errors
- Verify API endpoints are accessible: `http://localhost:8000/api/summary`
- Ensure database has data (run paper execution first)

### Styles not loading
- Check that `static/` folder exists and contains `css/style.css`
- Verify StaticFiles mount in `src/findmy/api/main.py`
- Clear browser cache (Ctrl+Shift+Del)

### Data not updating
- Check network tab in browser DevTools for failed requests
- Verify database queries in `src/findmy/api/main.py` endpoints
- Ensure Trade Service and SOT are properly seeded

## Testing

### Manual Testing Checklist
- [ ] Dashboard loads at http://localhost:8000/
- [ ] Positions table displays correctly
- [ ] Trade history shows all trades
- [ ] Summary cards update in real-time
- [ ] Refresh button works
- [ ] Auto-refresh works (data changes every 10 seconds)
- [ ] Responsive on mobile (viewport width < 768px)
- [ ] Swagger UI still available at /docs

### Automated Testing
```bash
pytest tests/test_dashboard_endpoints.py -v
pytest tests/test_api.py::test_dashboard_route -v
```

## Performance Considerations

### Optimization Tips
1. **Limit Trade History**: The dashboard loads the last 50 trades. For large datasets, consider pagination or filtering.
2. **Database Indexing**: Ensure indices on `trades.entry_time` and `trade_positions.updated_at`.
3. **Caching**: For heavy usage, consider caching summary data for 5-10 seconds.
4. **Pagination**: Future versions should implement paginated trade history.

### Scaling
For production with thousands of trades:
- Add pagination to `/api/trades` endpoint
- Implement server-side filtering and sorting
- Cache aggregated summary data
- Consider materialized views for performance metrics

## References

- [Bootstrap 5 Documentation](https://getbootstrap.com/docs/5.0/)
- [FastAPI Templates](https://fastapi.tiangolo.com/advanced/templates/)
- [Jinja2 Template Engine](https://jinja.palletsprojects.com/)
- [Chart.js](https://www.chartjs.org/) (for future chart integration)

---

**Last Updated**: 2026-01-12  
**Status**: ✅ Complete – v0.11.0 (Tab-based UI + Selective Updates)
