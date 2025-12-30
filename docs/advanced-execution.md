# Advanced Execution Simulation (v0.3.0)

## Overview

Version 0.3.0 introduces advanced paper trading simulation features including:
- **Partial Fill Support**: Orders can be filled incrementally over multiple simulations
- **Execution Costs**: Configurable fees and slippage simulation
- **Enhanced Reporting**: Detailed trade breakdown with aggregated metrics
- **Stop-Loss Orders**: Automated stop-loss order management with price triggers

## Features

### 1. Partial Fill Support

Orders are now filled in configurable increments (default: 100% per simulation for backward compatibility).

**Configuration:**
```python
DEFAULT_FILL_PCT = 1.0  # Fraction of remaining quantity to fill per call
```

**Example:**
```python
# With DEFAULT_FILL_PCT = 0.5, a 100 unit order fills as:
# First call: 50 units (remaining: 50)
# Second call: 25 units (remaining: 25)
# Third call: 12.5 units (remaining: 12.5)
```

**Database Tracking:**
- `remaining_qty`: Tracks unfilled quantity
- `order.status`: NEW → PARTIALLY_FILLED → FILLED

### 2. Execution Costs

Two types of execution costs are supported:

#### Slippage
Simulates adverse price movement when executing orders.

**Configuration:**
```python
DEFAULT_SLIPPAGE_PCT = 0.0  # Max slippage as percentage (0.0 = no slippage)
```

**How it works:**
- BUY orders: Price increases by random `[0, DEFAULT_SLIPPAGE_PCT]`
- SELL orders: Price decreases by random `[0, DEFAULT_SLIPPAGE_PCT]`
- `effective_price = price * (1 ± slippage_pct)`

#### Fees
Configurable maker and taker fees per order.

**Configuration:**
```python
DEFAULT_TAKER_FEE = 0.0    # 0% (no fees by default)
DEFAULT_MAKER_FEE = 0.0    # 0% (no fees by default)
```

**Per-Order Configuration:**
```python
order = Order(...)
order.taker_fee_rate = 0.001  # 0.1% fees on this order
```

**Calculation:**
```python
fees = effective_price * fill_qty * fee_rate
```

**Impact on PnL:**
- Fees are deducted from realized PnL for SELL orders
- Trade records include `fees` and `slippage_amount` fields

### 3. Enhanced Reporting

The `run_paper_execution()` function now returns detailed execution metrics:

```json
{
  "orders": 10,
  "trades": 12,
  "summary": {
    "total_fees": 10.5,
    "total_slippage": 5.25,
    "total_realized_pnl": 125.75
  },
  "positions": [
    {
      "symbol": "BTC/USD",
      "size": 5.0,
      "avg_price": 50000.0,
      "realized_pnl": 250.0
    }
  ],
  "errors": null
}
```

**Metrics Explained:**
- `total_fees`: Sum of all trading fees across all trades
- `total_slippage`: Sum of absolute slippage amounts (cost impact)
- `total_realized_pnl`: Cumulative realized profit/loss from closed positions

### 4. Stop-Loss Orders

Automated stop-loss order management with price-based triggers.

**Order Type:**
```python
order_type = "STOP_LOSS"  # New order type
stop_price = 45000.0       # Price at which to trigger sale
```

**Usage:**
```python
from findmy.execution.paper_execution import upsert_order, check_and_trigger_stoploss

# Create a stop-loss order
order, _ = upsert_order(
    session=session,
    client_order_id="STOP_001",
    symbol="BTC/USD",
    qty=5.0,
    price=50000.0,      # Fallback execution price if needed
    side="SELL",
    order_type="STOP_LOSS",
    stop_price=45000.0   # Trigger price
)

# Check and execute if triggered
triggered = check_and_trigger_stoploss(
    session,
    {"BTC/USD": 44500.0}  # Current prices
)
```

**Order Lifecycle:**
1. Created with `status="NEW"`, `order_type="STOP_LOSS"`
2. Checked against current market prices
3. If `current_price <= stop_price` (for SELL orders):
   - Status changes to `TRIGGERED`
   - Order is immediately executed at current market price
   - Trade recorded with `triggered_at_price` field

**Trade Fields:**
All trades include:
- `qty`, `filled_qty`, `remaining_qty`
- `price`: Original order price
- `effective_price`: Actual execution price (after slippage)
- `fees`: Transaction fees
- `slippage_amount`: Slippage cost
- For SELL orders: `realized_pnl`, `cost_basis`
- For stop-loss: `triggered_at_price`

## Configuration

All simulation parameters are configurable at the module level in `src/findmy/execution/paper_execution.py`:

```python
# Partial fills
DEFAULT_FILL_PCT = 1.0

# Execution costs
DEFAULT_SLIPPAGE_PCT = 0.0
DEFAULT_TAKER_FEE = 0.0
DEFAULT_MAKER_FEE = 0.0
```

## Database Schema

### Orders Table

New columns added in migration 004:
- `order_type` (VARCHAR): MARKET, LIMIT, or STOP_LOSS
- `stop_price` (NUMERIC): Stop price for stop-loss orders (nullable)

### Trades Table

Columns tracking execution costs (migration 003):
- `effective_price` (NUMERIC): Price after slippage
- `fees` (NUMERIC): Transaction fees charged
- `slippage_amount` (NUMERIC): Slippage cost

## API Integration

### FastAPI Endpoints

The `/api/paper-execution` endpoint now returns enhanced data:

```python
POST /api/paper-execution
Body: {"excel_path": "path/to/orders.xlsx"}

Response:
{
  "orders": int,
  "trades": int,
  "summary": {
    "total_fees": float,
    "total_slippage": float,
    "total_realized_pnl": float
  },
  "positions": [...],
  "errors": [...] or null
}
```

## Examples

### Example 1: Basic BUY/SELL with Slippage

```python
# Enable 0.1% slippage
DEFAULT_SLIPPAGE_PCT = 0.001

order = upsert_order(session, "001", "BTC/USD", 10.0, 50000.0, side="BUY")
success, trade = simulate_fill(session, order)

# Output might be:
# {
#   "effective_price": 50007.7,  # Slightly higher due to slippage
#   "slippage_amount": 77.0,      # 10 * 7.7
#   "price": 50000.0
# }
```

### Example 2: SELL with Fees and PnL

```python
# Enable 0.1% taker fee
DEFAULT_TAKER_FEE = 0.001

# Buy 10 at 50000
buy_order = upsert_order(session, "buy_001", "BTC/USD", 10.0, 50000.0, side="BUY")
simulate_fill(session, buy_order)

# Sell 5 at 55000
sell_order = upsert_order(session, "sell_001", "BTC/USD", 5.0, 55000.0, side="SELL")
success, trade = simulate_fill(session, sell_order)

# Output:
# {
#   "price": 55000.0,
#   "effective_price": 55000.0,
#   "qty": 5.0,
#   "fees": 27.5,  # 55000 * 5 * 0.001
#   "cost_basis": 250000.0,  # 50000 * 5
#   "realized_pnl": 22472.5   # (55000 - 50000) * 5 - 27.5
# }
```

### Example 3: Stop-Loss Order

```python
# Create position
buy = upsert_order(session, "buy", "BTC/USD", 5.0, 50000.0, side="BUY")
simulate_fill(session, buy)

# Place stop-loss at 45000
stop = upsert_order(
    session, "stop_001", "BTC/USD", 5.0, 50000.0,
    side="SELL",
    order_type="STOP_LOSS",
    stop_price=45000.0
)

# Check prices and trigger if reached
triggered = check_and_trigger_stoploss(session, {"BTC/USD": 44500.0})

# Output: Stop is triggered at 44500 (below 45000)
# Trade created with loss: (44500 - 50000) * 5 = -27500
```

## Testing

All features are covered by comprehensive tests in `tests/test_paper_execution.py`:

```bash
pytest tests/test_paper_execution.py -v
```

Key test suites:
- `TestSimulateFill`: Partial fill logic
- `TestSellOrderExecution`: Realized PnL calculation with fees
- `TestRunPaperExecution`: End-to-end execution workflow

## Migration Steps

For existing databases, apply migrations:

```bash
sqlite3 data/findmy_fm_paper.db < db/migrations/003_partial_fills_and_costs.sql
sqlite3 data/findmy_fm_paper.db < db/migrations/004_stop_loss_orders.sql
```

Or let SQLAlchemy handle it automatically on next run (new databases only).

## Future Enhancements

Planned features for v0.4.0:
- [ ] Asynchronous execution with latency simulation
- [ ] Market order vs limit order differentiation
- [ ] Maker/taker fee detection based on order book
- [ ] Multi-leg strategies (spread orders)
- [ ] Order cancellation and modification
- [ ] Advanced reporting dashboard with charts

## Backward Compatibility

All changes are backward compatible:
- Existing tests pass without modification
- Default values maintain previous behavior (full fills, no fees/slippage)
- New fields are optional in `upsert_order()`
- Existing order types default to MARKET
