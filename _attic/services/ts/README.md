# TS Service (Trade Service)

## Role

Trade Service aggregates, analyzes, and tracks executed trades from SOT (Source of Truth).
TS owns the trade lifecycle, P&L calculations, position inventory, and performance analytics.

## Responsibilities

### Trade Lifecycle Management
- **Open Trade** – Create new trade when entry order fills
- **Close Trade** – Record exit and calculate P&L
- **Partial Closure** – Handle partial exits and position reduction
- **Trade Attributes** – Track strategy, signal source, and attribution

### P&L Calculations
- **Gross P&L** – Entry price vs exit price
- **Fee Impact** – Entry and exit commissions/slippage
- **Net P&L** – Gross P&L minus fees
- **Return %** – (Net P&L / Cost Basis) × 100
- **Realized vs Unrealized** – Closed vs open trade P&L

### Position Tracking
- **Inventory State** – Current quantity per symbol
- **Average Entry Price** – Cost basis for position
- **Cumulative Stats** – Total traded, total cost
- **Position Reconciliation** – Validate against SOT

### Performance Analytics
- **Trade Metrics** – Win rate, avg win/loss, consecutive wins/losses
- **Time-bucketed Aggregation** – Hourly, daily, weekly performance
- **Risk Metrics** – Max profit, max loss, max drawdown per trade
- **Duration Tracking** – How long trades stay open

## Forbidden

TS **does not**:
- Make trading decisions (that's Strategy Engine)
- Fetch market data (that's market data service)
- Perform risk checks (that's Risk Management)
- Modify SOT data (read-only integration)
- Own raw order execution (that's SOT)

## Owned Data

### Immutable Tables (Fact Data)
| Table | Purpose | Lifecycle |
|-------|---------|-----------|
| `trades` | Aggregated entry → exit trades | Created on open, updated on close |
| `trade_pnl` | P&L snapshot per trade | Calculated and cached |
| `trade_positions` | Current position state | Updated after each trade |

### Derived Tables (Recomputable)
| Table | Purpose | Recalculation |
|-------|---------|---------------|
| `trade_performance` | Time-bucketed metrics | Daily/hourly aggregation job |

## Architecture

```
┌─────────────────────────────────────────┐
│ API Layer (ts/routes.py)                │
│ - Trade endpoints                       │
│ - Position endpoints                    │
│ - P&L endpoints                         │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│ TS Service (ts/service.py)              │
│ - Business logic                        │
│ - Trade lifecycle orchestration         │
│ - P&L calculations                      │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│ TS Repository (ts/repository.py)        │
│ - Data access                           │
│ - Query builders                        │
│ - SOT integration                       │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│ TS Models (ts/models.py)                │
│ - Trade                                 │
│ - TradePnL                              │
│ - TradePosition                         │
│ - TradePerformance                      │
│                                         │
│ + SOT Models (read-only)                │
│ - Order, OrderFill, OrderCost           │
└─────────────────────────────────────────┘
       ↓
   SQLite Database
```

## Data Flow

```
1. ENTRY
   SOT: order_requests → orders (filled)
   TS: open_trade() → creates Trade record

2. POSITION UPDATE
   TS: _update_position() → TradePosition
   (inventory tracking)

3. EXIT
   SOT: orders (exit filled)
   TS: close_trade() → Trade.status = CLOSED
       calculate_pnl() → TradePnL record

4. ANALYTICS
   TS: aggregate_performance() → TradePerformance
   (hourly/daily buckets)
```

## Key Operations

### Open Trade
```python
ts_service.open_trade(
    entry_order_id=123,
    symbol="AAPL",
    side="BUY",
    entry_qty=100,
    entry_price=150.50,
    strategy_code="momentum_001",
    signal_source="backtest"
)
```

### Close Trade
```python
ts_service.close_trade(
    trade_id=1,
    exit_order_id=124,
    exit_qty=100,
    exit_price=152.00
)
```

### Get Trade with P&L
```python
trade_data = ts_service.get_trade(trade_id=1)
# Returns: {
#   "id": 1,
#   "symbol": "AAPL",
#   "pnl": {
#     "net_pnl": 150.00,
#     "return_pct": 0.99
#   }
# }
```

### List Positions
```python
positions = ts_service.list_positions()
# Returns: [
#   {
#     "symbol": "AAPL",
#     "quantity": 50,
#     "avg_entry_price": 150.50,
#     "total_cost": 7525.00
#   }
# ]
```

## Integration with SOT

TS reads from SOT:
- **Order data** – Entry/exit orders for trades
- **Fills** – Execution details and prices
- **Costs** – Commissions and fees

TS never writes to SOT (one-way integration).

## Testing

See `tests/test_ts_*.py` for comprehensive test coverage:
- Trade lifecycle tests
- P&L calculation tests
- Position tracking tests
- SOT integration tests

## Next Steps (Phase 3+)

- **Risk Management** – Pre-trade risk checks
- **Backtesting** – Historical performance analytics
- **Strategy Engine** – Signal-to-trade automation
- **Live Trading** – Exchange integration
