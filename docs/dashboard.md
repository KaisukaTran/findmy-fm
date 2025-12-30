# FINDMY FM Dashboard

## Overview

The **FINDMY FM Dashboard** is a beautiful, responsive HTML interface that provides real-time monitoring of the Trade Service (TS) and Statement of Truth (SOT) systems. It replaces manual Swagger UI interaction with an intuitive visual experience.

## Features

### 📊 System Status
- **Database Connection**: Real-time verification of database connectivity
- **Trade Service Status**: Active/inactive state of the TS engine
- **SOT Audit Ready**: Verification that the Statement of Truth is audit-ready
- **Last Update Time**: Timestamp of the most recent trade execution

### 💼 Current Positions
A responsive table displaying all open positions:
- **Symbol**: Trading pair (e.g., BTC/USDT)
- **Quantity**: Current position size
- **Average Price**: Entry price or weighted average
- **Total Cost**: Total capital invested in the position

### 📈 Trade History
Complete record of all trades with real-time sorting:
- **Timestamp**: Entry time with date and time
- **Symbol**: Trading pair
- **Side**: BUY or SELL (color-coded badges)
- **Quantity**: Entry quantity
- **Price**: Entry execution price
- **Status**: OPEN, CLOSED, or PARTIAL (color-coded)
- **Realized P&L**: Profit/loss for closed trades (green/red)

### 💰 Summary Cards
Key performance metrics at a glance:
- **Total Trades**: Cumulative trade count
- **Realized P&L**: Profits from closed positions (green if positive, red if negative)
- **Unrealized P&L**: Current open position P&L (green if positive, red if negative)
- **Total Invested**: Total capital deployed across positions

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

The dashboard includes several auto-refresh mechanisms:

1. **JavaScript Poll**: Every 10 seconds, the dashboard polls the API endpoints to fetch updated data
2. **Full Page Refresh**: Every 30 seconds, the entire page reloads (useful for stylesheet/template changes)
3. **Manual Refresh**: Click the "Refresh" button in the header to force an immediate update

To adjust refresh intervals, edit `templates/dashboard.html` and modify these lines:
```javascript
// Reload data every 10 seconds
setInterval(() => {
    loadSummary();
    loadPositions();
    loadTrades();
}, 10000);

// Full page refresh every 30 seconds
setInterval(() => {
    location.reload();
}, 30000);
```

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

**Last Updated**: 2025-12-30  
**Status**: ✅ Complete – Ready for Phase 3 (Binance Integration)
