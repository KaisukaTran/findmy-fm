# FINDMY – Execution Engine

## Paper Trading (v1)

FINDMY's execution engine for v1 is a **deterministic paper trading simulator** that:
- Accepts order intents (buy/sell)
- Simulates instantaneous fills at specified prices
- Tracks positions and realized/unrealized P&L
- Persists all results to the Source of Truth (SOT) database

### Design Philosophy

**Determinism First**: Same input orders always produce identical fills and positions. No randomness, no market data lookups.

**Stateless Processing**: The execution engine is a pure function:
```
orders → execution_engine() → fills + positions
```

No side effects, no internal state. All results come from the order data.

---

## Order Lifecycle

### 1. **Order Intent**
- **Source**: Excel file upload or API request
- **Data**: Order ID, symbol, quantity, price, direction (BUY/SELL)
- **Storage**: Persisted as `order_requests` in SOT

### 2. **Validation**
- Format validation (required fields)
- Quantity > 0
- Price > 0
- Symbol format check

### 3. **Order Creation**
- Create `order` record in SOT
- Status: `PENDING_FILL`

### 4. **Fill Simulation**
- **v1 (Current)**: Immediate full-fill at order price
- **Future**: Support partial fills, slippage, latency simulation
- Store as `order_fill` record

### 5. **Position Update**
- Aggregate fills by symbol
- Calculate average cost basis
- Track size (quantity)
- Store as `position` record

### 6. **P&L Snapshot**
- Unrealized P&L = (market_price - cost_basis) × size
- Realized P&L = (exit_price - entry_price) × exited_quantity
- Persisted for audit trail

---

## Database Model

### Core Tables

#### `order_requests`
```sql
id: UUID (primary key)
client_order_id: str
symbol: str
side: str (BUY / SELL)
quantity: float
price: float
created_at: timestamp
```

#### `orders`
```sql
id: UUID (primary key)
order_request_id: UUID (foreign key)
status: str (PENDING_FILL, FILLED, PARTIAL, REJECTED)
symbol: str
quantity: float
price: float
filled_quantity: float
created_at: timestamp
```

#### `order_fills`
```sql
id: UUID (primary key)
order_id: UUID (foreign key)
fill_qty: float
fill_price: float
filled_at: timestamp
```

#### `positions`
```sql
id: UUID (primary key)
symbol: str
size: float
avg_cost: float
last_updated: timestamp
```

#### `trades` (Derived)
```sql
id: UUID (primary key)
entry_order_id: UUID
exit_order_id: UUID (nullable)
symbol: str
entry_price: float
exit_price: float (nullable)
size: float
realized_pnl: float (nullable)
```

### Append-Only Guarantee

- `orders`, `order_fills`, and `order_events` are **immutable**
- No UPDATE or DELETE operations on historical records
- Corrections are new records only
- Enables perfect audit trail and replay capability

---

## Known Limitations (v1)

| Limitation | Impact | Workaround | v Next |
|-----------|--------|-----------|---------|
| BUY only | Cannot close positions | Manual position tracking | v2 adds SELL |
| Immediate fills | Unrealistic execution | No slippage modeling | v3: simulate latency & slippage |
| No market data | Static prices only | Price must be in order | v4: fetch real prices |
| No partial fills | All-or-nothing | Orders always fully fill | v2: support partial fills |
| Single asset | One symbol per run | Multiple symbols in one upload | Built-in (multi-symbol upload works) |

---

## Roadmap to Live Execution

### v2 (Next)
- [ ] SELL order support (close positions)
- [ ] Partial fill simulation
- [ ] Execution costs (fees, slippage)
- [ ] Position averaging (multiple entries)

### v3
- [ ] Latency simulation (delayed fills)
- [ ] Slippage modeling (price impact)
- [ ] Order rejection rules
- [ ] Execution statistics (fill quality)

### v4
- [ ] Real market data integration
- [ ] Order cancellation & amendment
- [ ] Time-in-force rules (IOC, GTC, etc.)
- [ ] Async execution with execution_id tracking

### v5
- [ ] Multi-exchange support
- [ ] Real-time order management
- [ ] Broker-specific adapters (Binance, IB, etc.)
- [ ] Position reconciliation

### v6
- [ ] Smart order routing (best execution)
- [ ] Execution algorithms (TWAP, VWAP, etc.)
- [ ] Regulatory-grade audit logs
- [ ] Post-trade compliance checks

---

## Testing the Execution Engine

### Unit Tests
Located in `tests/` (future):
```python
from findmy.execution.paper_execution import PaperExecutionEngine

engine = PaperExecutionEngine()
orders = [Order(symbol="BTC/USDT", qty=0.5, price=65000)]
fills = engine.execute(orders)
assert len(fills) == 1
assert fills[0].qty == 0.5
```

### Integration Tests
Test end-to-end: Excel → Execution → SOT persistence
```bash
# Future command
PYTHONPATH=. pytest tests/test_execution_e2e.py -v
```

---

## Performance Notes

### Current Performance (v1)
- **Throughput**: Thousands of orders per second (in-memory)
- **Latency**: < 1ms per order (deterministic)
- **Memory**: Minimal (no state retention)

### Scalability Approach
- Execution is stateless → horizontally scalable
- Orders can be batched and processed in parallel
- Database is append-only → fast writes, sharded reads

---

## References

- **Architecture**: See [architecture.md](architecture.md)
- **API Contracts**: See [api.md](api.md)
- **Source of Truth**: See [SOT.md](SOT.md)
