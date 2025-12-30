# FINDMY – Execution Engine

## Paper Trading (v0.2.0)

FINDMY's execution engine is a **deterministic paper trading simulator** that:
- Accepts BUY and SELL order intents
- Simulates instantaneous fills at specified prices
- Tracks positions with cost basis averaging
- Calculates realized P&L on position closures
- Persists all results to the Source of Truth (SOT) database

### Design Philosophy

**Determinism First**: Same input orders always produce identical fills and positions. No randomness, no market data lookups.

**Stateless Processing**: The execution engine is a pure function:
```
orders → execution_engine() → fills + positions + realized_pnl
```

No side effects, no internal state. All results come from the order data.

---

## Order Lifecycle

### 1. **Order Intent**
- **Source**: Excel file upload or API request
- **Data**: Order ID, symbol, quantity, price, side (BUY/SELL)
- **Storage**: Persisted as `order_requests` in SOT

### 2. **Validation**
- Format validation (required fields)
- Side validation (BUY or SELL)
- Quantity > 0
- Price > 0
- Symbol format check
- **For SELL**: Position sufficiency check (must have enough quantity)

### 3. **Order Creation**
- Create `order` record in SOT
- Status: `NEW`

### 4. **Fill Simulation**
- **Current (v0.2.0)**: Immediate full-fill at order price
- **Future**: Support partial fills, slippage, latency simulation
- Store as `trade` record with side indicator

### 5. **Position Update**

#### For BUY Orders:
- Add quantity to existing position (or create new position)
- Update average cost basis: `(old_qty * old_avg + new_qty * new_price) / total_qty`
- Increase position size

#### For SELL Orders:
- Reduce position by sold quantity
- **Calculate realized P&L**: `(sell_price - cost_basis) × sold_qty`
- Update cumulative realized PnL
- If position fully closed: set size to 0, avg_price to 0
- If position partially closed: maintain cost basis for remaining shares

### 6. **P&L Snapshot**
- **Realized P&L**: (exit_price - entry_price) × closed_quantity (recorded on SELL)
- **Unrealized P&L**: (market_price - cost_basis) × remaining_size (for open positions)
- Persisted for audit trail and reporting

---

## SELL Order Flow (v0.2.0)

### Example: Full Lifecycle

```
Step 1: BUY 10 BTC @ $100
  → Position: {size: 10, avg_price: 100, realized_pnl: 0}

Step 2: SELL 5 BTC @ $110
  → Realized PnL: (110 - 100) × 5 = $50
  → Position: {size: 5, avg_price: 100, realized_pnl: 50}

Step 3: SELL 3 BTC @ $120
  → Realized PnL: (120 - 100) × 3 = $60
  → Position: {size: 2, avg_price: 100, realized_pnl: 110}

Step 4: SELL 2 BTC @ $130
  → Realized PnL: (130 - 100) × 2 = $60
  → Position: {size: 0, avg_price: 0, realized_pnl: 170}
  → Position fully closed
```

### Oversell Prevention

- **Error Check**: If sell_qty > current_position_size, raise ValueError
- **Clear Message**: "Insufficient position for SELL: requested X, current position Y"
- **Processing**: Row is skipped with error logged; no partial execution

---

## Database Model (v0.2.0)

### Core Tables

#### `orders`
```sql
id: int (primary key)
client_order_id: str (unique)
symbol: str
side: str (BUY or SELL)  -- NEW in v0.2.0
qty: float
price: float
status: str (NEW, FILLED, CANCELLED)
created_at: timestamp
updated_at: timestamp
```

#### `trades`
```sql
id: int (primary key)
order_id: int (foreign key → orders.id)
symbol: str
side: str (BUY or SELL)  -- NEW in v0.2.0
qty: float
price: float
ts: timestamp
```

#### `positions` -- UPDATED in v0.2.0
```sql
id: int (primary key)
symbol: str (unique)
size: float (current position size)
avg_price: float (cost basis for remaining shares)
realized_pnl: float (NEW) -- cumulative realized P&L from closed positions
updated_at: timestamp
```

### Data Integrity

- All orders and trades are immutable (append-only)
- Position is updated atomically with trade
- Realized PnL is cumulative and monotonic (only increases or stays same)

---

## Excel Format Support

### With Header (Recommended)

```
Order ID | Quantity | Price  | Trading Pair | Side
---------|----------|--------|--------------|------
001      | 10       | 100.00 | BTC/USD      | BUY
002      | 5        | 110.00 | BTC/USD      | SELL
003      | 3        | 105.00 | ETH/USD      | BUY
```

**Supported Column Names**:
- Order ID: "Order ID", "stt", "client_id"
- Quantity: "Quantity", "qty"
- Price: "Price"
- Trading Pair: "Trading Pair", "symbol", "pair"
- Side (optional): "Side", "order side", "direction"

### Without Header (Positional)

```
A          | B  | C      | D        | E (optional)
-----------|----|----|---------|-------
001        | 10 | 100.00 | BTC/USD  | BUY
002        | 5  | 110.00 | BTC/USD  | SELL
```

- Column A: Order ID
- Column B: Quantity
- Column C: Price
- Column D: Trading Pair
- Column E (optional): Side (defaults to BUY)

### Side Detection (Vietnamese + English)

- **BUY**: "BUY", "buy" (English) or "MUA", "mua" (Vietnamese)
- **SELL**: "SELL", "sell" (English) or "BÁN", "bán" (Vietnamese)
- **Default**: If no side specified or unrecognized, defaults to "BUY"

---

## Known Limitations (v0.2.0)

| Limitation | Impact | Workaround | Next Version |
|-----------|--------|-----------|---------|
| Immediate fills | Unrealistic execution | No slippage modeling | v0.3: simulate latency & slippage |
| No market data | Static prices only | Price must be in order | v0.4: fetch real prices |
| No partial fills | All-or-nothing | Orders always fully fill | v0.3: support partial fills |
| No fees/slippage | Unrealistic P&L | Manual adjustment | v0.3: model execution costs |

---

## Roadmap to Live Execution

### v0.2.0 (Current) ✅
- [x] SELL order support with position reduction
- [x] Realized P&L calculation
- [x] Oversell prevention
- [x] Excel side detection (English + Vietnamese)
- [x] Full test coverage (38 tests)

### v0.3.0 (Next)
- [ ] Partial fill simulation
- [ ] Execution costs (fees, slippage modeling)
- [ ] Latency simulation (delayed fills)
- [ ] Take-profit & stop-loss orders

### v0.4.0
- [ ] Real market data integration
- [ ] Order cancellation & amendment
- [ ] Time-in-force rules (IOC, GTC, FOK)
- [ ] Async execution with execution_id tracking

### v0.5.0+
- [ ] Multi-exchange support
- [ ] Smart order routing
- [ ] Regulatory-grade audit logs
- [ ] Post-trade compliance checks

---

## Examples

### Example 1: Simple BUY

**Input** (Excel):
```
Order ID | Quantity | Price | Trading Pair | Side
001      | 10       | 100   | BTC/USD      | BUY
```

**Output**:
```json
{
  "orders": 1,
  "trades": 1,
  "positions": [
    {
      "symbol": "BTC/USD",
      "size": 10.0,
      "avg_price": 100.0,
      "realized_pnl": 0.0
    }
  ],
  "errors": null
}
```

### Example 2: BUY then SELL (Profit)

**Input** (Excel):
```
Order ID | Quantity | Price | Trading Pair | Side
001      | 10       | 100   | BTC/USD      | BUY
002      | 10       | 110   | BTC/USD      | SELL
```

**Output**:
```json
{
  "orders": 2,
  "trades": 2,
  "positions": [
    {
      "symbol": "BTC/USD",
      "size": 0.0,
      "avg_price": 0.0,
      "realized_pnl": 100.0
    }
  ],
  "errors": null
}
```

### Example 3: Oversell Error

**Input** (Excel):
```
Order ID | Quantity | Price | Trading Pair | Side
001      | 5        | 100   | BTC/USD      | BUY
002      | 10       | 110   | BTC/USD      | SELL
```

**Output**:
```json
{
  "orders": 2,
  "trades": 1,
  "positions": [
    {
      "symbol": "BTC/USD",
      "size": 5.0,
      "avg_price": 100.0,
      "realized_pnl": 0.0
    }
  ],
  "errors": [
    {
      "row": 3,
      "error": "Insufficient position for SELL: requested 10, current position 5 for BTC/USD"
    }
  ]
}
```

---

## Testing

### Run Tests
```bash
pytest tests/test_paper_execution.py -v
```

### Test Coverage
- 38 tests covering:
  - Excel parsing (with/without headers, Vietnamese/English side detection)
  - BUY order execution
  - SELL order execution
  - Position reduction & realized P&L
  - Oversell prevention
  - Mixed BUY/SELL workflows
  - Error handling

---

## References

- **API**: See [api.md](api.md)
- **Configuration**: See [configuration.md](configuration.md)
- **Database Schema**: See [database-schema.md](database-schema.md)
