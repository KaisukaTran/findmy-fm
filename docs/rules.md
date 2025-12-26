# FINDMY – Architectural Rules & Constraints

## Overview

These rules define the architectural constraints and design principles that guide FINDMY's evolution. They ensure maintainability, auditability, and scalability.

---

## Core Rules

### Rule 1: Separation of Concerns
Each module has **one clear responsibility**:

- **API**: HTTP interface only (no trading logic)
- **Execution**: Trading logic only (no persistence)
- **SOT**: Data storage only (no decisions)
- **Strategy**: Signal generation only (no execution)

**Violation Example** ❌:
```python
# Bad: API contains trading logic
class TradeAPI:
    def execute_trade(self, order):
        # Calculate fills here (logic)
        fill = self._simulate_fill(order)
        # Persist here (storage)
        self.db.save(fill)
        return fill
```

**Correct Example** ✅:
```python
# Good: Separation
# api.py
@app.post("/execute")
def execute(order):
    result = execution_engine.execute(order)  # Delegate logic
    sot.save(result)  # Delegate storage
    return result

# execution_engine.py
def execute(order):
    # Only logic
    return fill

# sot.py
def save(result):
    # Only storage
    db.insert(result)
```

---

### Rule 2: Determinism

**All trading decisions must be deterministic**: Same input → Same output.

**No randomness**:
- ❌ `random.random()`
- ❌ `np.random.choice()`
- ❌ Current time (`datetime.now()`)
- ❌ External API calls (non-deterministic responses)

**No future data**:
- ❌ Look-ahead bias in strategies
- ❌ Using data from future bars
- ❌ Peeking at tomorrow's price

**Allows**:
- ✅ Input market data (historical, fixed)
- ✅ Fixed parameters
- ✅ Deterministic functions
- ✅ External data from past (not future)

**Why**: Enables backtesting, debugging, and replay.

---

### Rule 3: Immutability of Facts

**Historical facts never change; corrections are new records.**

### Examples of Facts
- Orders placed
- Fills executed
- Trades closed
- Risk validation results

### Examples of Derived Data
- Current positions (can change)
- P&L snapshots (can be recalculated)
- Performance metrics (can be updated)

**Violation** ❌:
```python
# Bad: Updating an order
order = db.query(Order).get(123)
order.status = 'CANCELLED'  # Modifying fact
db.update(order)
```

**Correct** ✅:
```python
# Good: Creating new event record
order = db.query(Order).get(123)
event = OrderEvent(
    order_id=order.id,
    event_type='CANCELLED',
    created_at=now()
)
db.insert(event)
```

---

### Rule 4: Single Source of Truth (SOT)

**All modules read from SOT; SOT doesn't read from other modules.**

```
API → (reads) → SOT
Execution → (writes) → SOT
Analytics → (reads) → SOT
Audit → (reads) → SOT
```

**Not allowed**:
- ❌ SOT querying API
- ❌ API maintaining its own copy of orders
- ❌ Execution engine caching positions
- ❌ Strategy reading directly from API

**Violation** ❌:
```python
# Bad: API maintains its own cache
class API:
    def __init__(self):
        self.orders_cache = {}  # Duplicate of truth!
    
    def get_orders(self):
        return self.orders_cache  # Stale!
```

**Correct** ✅:
```python
# Good: Always read from SOT
class API:
    def get_orders(self):
        return sot.query_orders()  # Current!
```

---

### Rule 5: Stateless Components

**Execution and strategy engines must be stateless.**

**No instance variables** that persist between calls:
- ❌ `self.position = ...`
- ❌ `self.filled_count = ...`
- ❌ `self.last_price = ...`

**All context comes from inputs**:
- ✅ Function parameters
- ✅ Input market data
- ✅ Current portfolio state

**Why**: Enables parallelization, testing, and cloud deployment.

**Violation** ❌:
```python
class BadExecutor:
    def __init__(self):
        self.positions = {}  # State!
    
    def execute(self, orders):
        for order in orders:
            self.positions[order.symbol] = ...  # Mutating!
```

**Correct** ✅:
```python
def execute(orders, current_positions):  # State as input
    new_positions = {}
    for order in orders:
        new_positions[order.symbol] = ...  # New output
    return new_positions  # Returned, not stored
```

---

### Rule 6: No Coupling Across Layers

**Lower layers don't depend on upper layers.**

```
Layer 3: API       (highest level)
Layer 2: Execution (business logic)
Layer 1: SOT       (data layer)

Allowed: 3 → 2 → 1
Forbidden: 1 → 2, 1 → 3, 2 → 3
```

**Violation** ❌:
```python
# sot.py (Layer 1) imports from api (Layer 3)
from findmy.api.schemas import ExecutionResponse
```

**Correct** ✅:
```python
# api.py (Layer 3) imports from sot (Layer 1)
from services.sot import repository
```

---

### Rule 7: Explicit Error Handling

**Errors must be explicit and handleable.**

**Not allowed**:
- ❌ Silent failures (logging without raising)
- ❌ Generic `Exception` (too broad)
- ❌ Swallowing exceptions

**Required**:
- ✅ Custom exception types
- ✅ Meaningful error messages
- ✅ Proper HTTP status codes
- ✅ Audit trail of errors

**Example**:
```python
# Good
class InvalidOrderError(Exception):
    """Order failed validation."""
    pass

def validate_order(order):
    if order.qty <= 0:
        raise InvalidOrderError(f"qty must be positive, got {order.qty}")

# API catches and converts
try:
    result = execute(order)
except InvalidOrderError as e:
    return error_response(400, "INVALID_ORDER", str(e))
```

---

### Rule 8: Auditability

**Every significant action must be auditable.**

**Must be recorded**:
- ✅ Orders created
- ✅ Orders rejected (and why)
- ✅ Fills executed
- ✅ Risk checks performed
- ✅ Errors encountered

**Immutable audit trail**:
- ✅ Append-only event log
- ✅ Timestamps
- ✅ User/agent identification
- ✅ Context (market state, decision reason)

**Example**:
```python
# Record with context
event = OrderEvent(
    order_id=order.id,
    event_type='FILLED',
    fill_qty=order.qty,
    fill_price=65000,
    market_conditions={
        'rsi': 28.5,
        'sma_200': 64500,
        'volatility': 0.42
    },
    decision_reason="Mean reversion signal",
    created_at=now()
)
sot.record_event(event)
```

---

## Anti-Patterns

### ❌ API Contains Business Logic
```python
# Bad
@app.post("/execute")
def execute(file):
    # Parsing (OK)
    orders = parse_excel(file)
    # Logic (NOT OK – should be in execution layer)
    fills = []
    for order in orders:
        fills.append(simulate_fill(order))
    return fills
```

### ❌ Execution Persists Directly
```python
# Bad
def execute(orders):
    fills = simulate_fills(orders)
    db.insert(fills)  # NOT OK – should return, let API persist
    return fills
```

### ❌ SOT Makes Decisions
```python
# Bad
def insert_order(order):
    if self.should_execute(order):  # NOT OK – decision logic
        self.execute(order)
    self.store(order)
```

### ❌ Stateful Strategy
```python
# Bad
class Strategy:
    def __init__(self):
        self.position_count = 0
    
    def signal(self, data):
        self.position_count += 1  # State mutation
        return Signal(...)
```

---

## Enforcement

### Code Review Checklist

Before merging, verify:
- [ ] Each module has clear single responsibility
- [ ] No business logic in API layer
- [ ] No persistence in execution layer
- [ ] No data access in strategy layer
- [ ] Execution and strategy are stateless
- [ ] Lower layers don't import upper layers
- [ ] All errors are explicit and handled
- [ ] Audit trail is complete
- [ ] Determinism preserved (no `random`, `now()`, API calls)

### Testing

- **Unit tests**: Verify determinism (same input → same output)
- **Integration tests**: Verify separation of concerns
- **Audit tests**: Verify all decisions recorded
- **Replay tests**: Verify backtest matches production

---

## Migration Path

If existing code violates these rules:

1. **Document violation**: Why does it exist?
2. **Understand impact**: What breaks if we fix it?
3. **Plan refactoring**: How to migrate safely?
4. **Implement gradual**: Deprecate, migrate, remove
5. **Test thoroughly**: Verify behavior unchanged

---

## When to Break Rules

**Exceptional cases** (rare):
- Performance critical path → measure, document, escalate
- Backwards compatibility → deprecation period, migration plan
- Legal/compliance → document exception, risk review

**Escalation process**:
1. Document the exception
2. Get architectural review
3. Set sunset date
4. Monitor compliance

---

## References

- **Architecture**: See [architecture.md](architecture.md)
- **Code Examples**: See [modules.md](modules.md)
- **Contributing**: See [CONTRIBUTING.md](CONTRIBUTING.md)
