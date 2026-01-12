# FINDMY Copilot Instructions

## Task Decomposition (Mandatory)

- **Always decompose large or complex tasks into smaller, sequential steps**
- Prefer incremental changes over large one-shot implementations
- Each step should:
  - Have a clear goal
  - Touch a limited number of files (ideally 1-3)
  - Be verifiable before proceeding to the next step
- Use todo lists to track multi-step work

## Response Length Safety

- Avoid generating large files or multi-module changes in a single response
- For long outputs (refactors, schemas, migrations), follow this pattern:
  1. **Propose a plan** — outline steps and affected files
  2. **Wait for confirmation** — user approves or adjusts
  3. **Implement step-by-step** — one logical change at a time
- If a file exceeds ~200 lines of changes, split into multiple edits

---

## Architecture Overview

FINDMY is a **modular trading system** with strict separation of concerns:

```
src/findmy/api/     → FastAPI HTTP layer (no business logic)
src/findmy/execution/ → Paper/live trading execution (stateless, deterministic)
src/findmy/kss/     → KSS Pyramid DCA strategy (session management)
services/sot/       → Source of Truth (SQLite, append-only facts)
services/ts/        → Trade Service (P&L, positions, read-only from SOT)
services/auth/      → JWT authentication
services/cache/     → L1/L2 caching layer
services/risk/      → Pre-trade risk checks (pip sizing, position limits)
```

**Key principle**: SOT is the single source of truth. All modules read from SOT; only execution writes to SOT.

## Data Flow: Order Lifecycle

All orders flow through manual approval:
```
Input → queue_order() → pending_orders table → Approve/Reject → Execution → SOT
```

Use `services/sot/pending_orders_service.py`:
```python
from services.sot.pending_orders_service import queue_order
order, risk_note = queue_order(symbol="BTC", side="BUY", pips=5, price=65000.0)
# risk_note is None if passed, or string with violation reason
```

## Database Pattern

- **Two separate databases**: `db/sot.db` (orders, fills, pending_orders) and `db/ts.db` (trades, P&L)
- Models use SQLAlchemy ORM with explicit indexes (see `services/sot/models.py`)
- Use `ScopedSession` for thread-safe access; `SessionLocal` for simple cases
- Facts are **append-only** — never update/delete historical records

```python
# Thread-safe pattern
from services.sot.db import ScopedSession, remove_session
session = ScopedSession()
try:
    # query/insert
finally:
    remove_session()

# Simple pattern (single-thread)
from services.sot.db import SessionLocal
db = SessionLocal()
try:
    # query/insert
    db.commit()
finally:
    db.close()
```

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=src/findmy --cov=services --cov-report=html

# Run specific test file
pytest tests/test_kss.py -v
```

- Fixtures in `conftest.py` auto-create database schemas
- Use `mock_market_data` fixture for price data
- Tests must be deterministic — no external API calls

## Development Commands

```bash
# Start API server (PYTHONPATH is critical)
./scripts/start_api.sh
# Or manually:
PYTHONPATH=src uvicorn findmy.api.main:app --host 0.0.0.0 --port 8000 --reload

# Code formatting
black src/ services/ tests/
ruff check src/ services/ --fix

# Type checking
mypy src/ --check-untyped-defs
```

## Conventions

### Commit Messages
```
feat(module): description    # New feature
fix(module): description     # Bug fix
docs(module): description    # Documentation
refactor(module): description # Code refactoring
test(module): description    # Tests only
```

### File Organization
- API routes: `src/findmy/api/sot/routes.py`, `src/findmy/kss/routes.py`
- Pydantic schemas: `**/schemas.py`
- SQLAlchemy models: `**/models.py`
- Business logic: `**/service.py`
- Database queries: `**/repository.py`

### Configuration
- All settings via `src/findmy/config.py` (pydantic-settings)
- Secrets use `SecretStr` — never log raw values
- Environment variables override `.env` file

## Risk Management

All orders pass through risk checks before queuing:
```python
from services.risk import calculate_order_qty, check_all_risks

# Convert pips to quantity (uses pip_multiplier from config)
qty = calculate_order_qty("BTC", pips=5)  # 5 pips × multiplier × minQty

# Check position limits and daily loss
passed, violations = check_all_risks("BTC", qty, db_session)
```

Settings in `src/findmy/config.py`: `pip_multiplier`, `max_position_size_pct`, `max_daily_loss_pct`

## KSS Pyramid Strategy

The KSS module (`src/findmy/kss/`) implements wave-based DCA:
- `pyramid.py` — Core `PyramidSession` dataclass and wave calculation
- `manager.py` — Session lifecycle (create/start/stop)
- `repository.py` — Database persistence
- `routes.py` — 8 REST endpoints under `/api/v1/kss/`
- `hooks.py` — Integration with pending_orders (on_order_approved/rejected)

Wave formulas:
```python
qty(n) = (n + 1) × pip_size
price(n) = entry_price × (1 - distance_pct/100)^n
```

## Common Pitfalls

1. **Don't import SOT models into TS** — causes circular imports; use order IDs
2. **Always set `PYTHONPATH=src`** when running scripts
3. **Use lazy imports** for cross-module dependencies (see `_get_kss_hooks()` in pending_orders_service.py)
4. **Dashboard uses WebSocket** at `/ws/dashboard` for real-time updates
5. **Imports from services/** use `from services.X` not `from src.findmy.services.X`

Wave formulas:
```python
qty(n) = (n + 1) × pip_size
price(n) = entry_price × (1 - distance_pct/100)^n
```

## Common Pitfalls

1. **Don't import SOT models into TS** — causes circular imports; use order IDs
2. **Always set `PYTHONPATH=src`** when running scripts
3. **Use repository pattern** for database queries, not raw SQL
4. **Dashboard uses WebSocket** at `/ws/dashboard` for real-time updates
