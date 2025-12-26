# FINDMY – Module Reference

## Directory Structure

```
services/                   # Microservices
├── sot/                    # Source of Truth service
│   ├── db.py              # Database configuration
│   ├── models.py          # SQLAlchemy ORM models
│   ├── repository.py      # Data access layer
│   ├── service.py         # Business logic
│   ├── routes.py          # API routes
│   ├── __init__.py
│   └── README.md
│
├── ts/                     # Trade Service (Phase 2) ✅ NEW
│   ├── db.py              # Database session management
│   ├── models.py          # Trade, TradePnL, TradePosition, TradePerformance
│   ├── repository.py      # TSRepository with 20+ methods
│   ├── service.py         # TSService with trade lifecycle
│   ├── routes.py          # 10 REST API endpoints
│   ├── __init__.py
│   └── README.md
│
├── ai/                     # AI/ML service (future)
├── dal/                    # Data aggregation layer (future)
├── executor/               # Execution service (future)
├── kss/                    # Key situation service (future)
├── report/                 # Reporting service (future)
└── __init__.py

src/findmy/
├── api/                     # FastAPI application
│   ├── __init__.py
│   ├── main.py             # App factory & startup
│   ├── app.py              # Route handlers
│   ├── schemas.py          # Pydantic models
│   ├── common/             # Shared utilities
│   │   ├── errors.py       # Exception classes
│   │   ├── handlers.py     # Error handlers
│   │   ├── middleware.py   # HTTP middleware
│   │   ├── enums.py        # Enum definitions
│   │   └── __init__.py
│   └── sot/                # SOT API routes
│       ├── routes.py       # Endpoint definitions
│       ├── schemas.py      # Request/response models
│       └── __init__.py
│
├── core/                    # Core business logic
│   └── [future modules]
│
├── data/                    # Data models & types
│   └── [future modules]
│
├── execution/              # Trading execution
│   ├── paper_execution.py  # Paper trading engine
│   └── __init__.py
│
├── persistence/            # Data layer abstractions
│   └── [future modules]
│
├── strategies/             # Trading strategies
│   └── [future modules]
│
└── __init__.py
```

---

## Module Descriptions

### `api.main`

**Purpose**: FastAPI application factory and startup configuration.

**Key Functions**:
- `create_app()`: Creates and configures the FastAPI application
- Registers all route blueprints
- Sets up middleware and error handlers
- Configures CORS and logging

**Example**:
```python
from findmy.api.main import create_app

app = create_app()
# Run: uvicorn findmy.api.main:app --reload
```

---

### `api.app`

**Purpose**: HTTP route handlers and request/response logic.

**Key Endpoints**:
- `GET /`: Health check
- `POST /paper-execution`: Execute paper trades from Excel

**Logic**:
- Receives HTTP requests
- Validates input (file format, data)
- Calls execution engine
- Returns structured responses

**Key Functions**:
- `health_check()`: Simple status check
- `execute_paper_trading(file)`: Main execution endpoint

---

### `api.schemas`

**Purpose**: Pydantic models for request/response validation.

**Models**:
- `ExecutionRequest`: File upload contract
- `ExecutionResponse`: Execution result contract
- `PositionSchema`: Individual position representation
- `TradeSchema`: Individual trade representation
- `ErrorResponse`: Error contract

**Usage**:
```python
from findmy.api.schemas import ExecutionResponse

response = ExecutionResponse(
    status="success",
    positions=[...],
    trades=[...]
)
```

---

### `api.common.errors`

**Purpose**: Custom exception classes for error handling.

**Exception Types**:
- `InvalidExcelFormat`: Malformed Excel file
- `InvalidOrderData`: Order validation failure
- `ExecutionError`: Engine execution failure
- `DatabaseError`: SOT persistence failure

**Usage**:
```python
from findmy.api.common.errors import InvalidExcelFormat

raise InvalidExcelFormat("Sheet 'purchase order' not found")
```

---

### `api.common.handlers`

**Purpose**: Global exception handlers for FastAPI.

**Features**:
- Catches all exceptions
- Converts to standardized error responses
- Logs errors with context
- Returns appropriate HTTP status codes

---

### `api.common.middleware`

**Purpose**: HTTP middleware for cross-cutting concerns.

**Features** (future):
- Request logging
- Response timing
- Correlation IDs
- Security headers

---

### `api.common.enums`

**Purpose**: Enumeration definitions for type safety.

**Enums**:
- `OrderSide`: BUY, SELL (future)
- `OrderStatus`: PENDING, FILLED, REJECTED
- `ExecutionStatus`: SUCCESS, ERROR, PARTIAL

---

### `api.sot.routes`

**Purpose**: Source of Truth API endpoints.

**Endpoints**:
- `GET /sot/orders`: List all orders
- `GET /sot/orders/{id}`: Get specific order
- `GET /sot/positions`: Current positions
- `GET /sot/trades`: Trade history

(Future: Full CRUD API for SOT)

---

### `api.sot.schemas`

**Purpose**: SOT-specific Pydantic models.

**Models**:
- `OrderSchema`: Order representation
- `PositionSchema`: Position snapshot
- `TradeSchema`: Trade record
- `PnLSchema`: P&L calculation

---

### `execution.paper_execution`

**Purpose**: Deterministic paper trading engine.

**Key Classes**:
- `PaperExecutionEngine`: Main execution orchestrator
- `Order`: Order representation
- `Fill`: Fill representation
- `Position`: Position tracking

**Key Methods**:
- `execute(orders: List[Order]) -> ExecutionResult`: Process orders
- `simulate_fills(orders) -> List[Fill]`: Generate fills
- `calculate_positions(fills) -> Dict[str, Position]`: Aggregate positions

**Design**:
- Pure function: inputs → outputs (no side effects)
- Deterministic: no randomness, same input = same result
- Testable: can be tested without database

**Example**:
```python
from findmy.execution.paper_execution import PaperExecutionEngine, Order

engine = PaperExecutionEngine()
orders = [
    Order(symbol="BTC/USDT", qty=0.5, price=65000)
]
result = engine.execute(orders)
print(f"Positions: {result.positions}")
```

---

## Module Dependency Map

```
API Layer (User-facing)
    ↓
api.main (FastAPI app)
    ├─ routes: api.app
    ├─ schemas: api.schemas
    └─ middleware: api.common.*
        ↓
Execution Layer (Business logic)
    ├─ execution.paper_execution
    └─ api.sot.routes
        ↓
Persistence Layer (Data storage)
    └─ services.sot (SQLAlchemy ORM)
```

---

## Future Modules

### `core/`
- Strategy interfaces
- Signal definitions
- Risk rule engine
- Portfolio manager

### `data/`
- Order domain models
- Trade domain models
- Position domain models
- Market data models

### `persistence/`
- Repository pattern implementations
- Database access layer
- Query builders
- Cache layer

### `strategies/`
- Strategy base class
- Example strategies
- Signal generators
- Backtest adapters

---

## Adding New Modules

1. Create file in appropriate directory
2. Start with docstring explaining purpose
3. Define clear interfaces (classes/functions)
4. Write unit tests in `tests/`
5. Update this document
6. Add to appropriate section in root README

---

## Coding Standards

### Import Organization
```python
# 1. Standard library
import json
from datetime import datetime

# 2. Third-party
import pandas as pd
from sqlalchemy import Column, String

# 3. Local
from findmy.api.schemas import ExecutionResponse
from findmy.execution.paper_execution import PaperExecutionEngine
```

### Naming Conventions
- **Classes**: PascalCase (`OrderRequest`, `ExecutionEngine`)
- **Functions**: snake_case (`execute_orders()`, `calculate_pnl()`)
- **Constants**: UPPER_SNAKE_CASE (`MAX_ORDERS = 1000`)
- **Private members**: `_leading_underscore()`

### Docstring Format
```python
def execute_orders(orders: List[Order]) -> ExecutionResult:
    """
    Execute orders deterministically.
    
    Args:
        orders: List of order requests to execute.
        
    Returns:
        ExecutionResult with fills and positions.
        
    Raises:
        ExecutionError: If execution fails.
    """
```

---

## Trade Service Module (`services/ts/`) ✅ – Phase 2

**Purpose**: Aggregate trades from SOT and provide P&L calculations, position tracking, and trade lifecycle management.

**Status**: ✅ Complete (Phase 2)

**Key Responsibilities**:
- Read-only integration with SOT (Order, OrderFill, OrderCost)
- Trade aggregation (entry order → exit order)
- P&L calculations (gross, net, realized, unrealized)
- Position inventory tracking with cost basis averaging
- Trade lifecycle management (OPEN → PARTIAL → CLOSED)
- Performance metrics and analytics

**Architecture**:
```
API Routes → Service Layer → Repository Layer → Models
(FastAPI)   (Business Logic) (Data Access)    (SQLAlchemy ORM)
```

**Database Models**:
- **Trade**: Entry/exit order pairs, status tracking, strategy info
- **TradePnL**: P&L snapshots with fees and performance metrics
- **TradePosition**: Inventory state with average price and cumulative costs
- **TradePerformance**: Time-bucketed metrics (daily/hourly stats)

**API Endpoints** (10 total):
- `POST /api/v1/ts/trades/open`: Open new trade
- `POST /api/v1/ts/trades/{id}/close`: Close/partial close trade
- `GET /api/v1/ts/trades/{id}`: Get trade details
- `GET /api/v1/ts/trades`: List trades (with filters)
- `GET /api/v1/ts/trades/{id}/pnl`: Get trade P&L
- `GET /api/v1/ts/pnl/total`: Aggregate P&L
- `GET /api/v1/ts/positions/{symbol}`: Get position inventory
- `GET /api/v1/ts/positions`: List all positions
- `GET /api/v1/ts/health`: Service health check

**Key Methods** (TSService):
- `open_trade()`: Create new trade, initialize P&L, update position
- `close_trade()`: Record exit, calculate P&L, update position
- `get_trade()`, `list_trades()`: Trade queries with filters
- `get_trade_pnl()`, `get_total_pnl()`: P&L queries
- `get_position()`, `list_positions()`: Position queries
- `_calculate_trade_pnl()`: P&L math (gross, net, fees, return %)
- `_update_position()`: Position inventory updates

**P&L Calculation**:
```
Cost Basis = entry_qty × entry_price
Gross P&L = (exit_price - entry_price) × exit_qty [inverted for SELL]
Total Fees = entry_fees + exit_fees (from SOT OrderCost)
Net P&L = Gross P&L - Total Fees
Return % = (Net P&L / Cost Basis) × 100
```

**Position Tracking**:
- BUY: Increases quantity, averages entry price
- SELL: Decreases quantity, maintains average price
- Cost basis: Weighted average of all entries

**Test Coverage**: 14/14 tests passing
- 3 trade lifecycle tests
- 3 P&L calculation tests  
- 2 position tracking tests
- 3 trade query tests
- 2 SOT integration tests
- 1 end-to-end workflow test

**Design Principles**:
- **Read-only SOT integration**: Never writes to SOT
- **Separation of concerns**: API ↔ Service ↔ Repository ↔ Models
- **Testable**: Comprehensive unit and integration tests
- **Extensible**: Easy to add new metrics, calculations, endpoints

---

## Related Documentation

- **Architecture**: See [architecture.md](architecture.md)
- **API Reference**: See [api.md](api.md)
- **Execution Details**: See [execution.md](execution.md)
