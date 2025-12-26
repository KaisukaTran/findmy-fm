# FINDMY – System Architecture

## Overview

FINDMY (FM) is a modular, research-first trading system designed to evolve from paper trading to live execution. The architecture prioritizes:

- **Deterministic execution**: Same inputs always produce identical results
- **Auditability**: All trading decisions and outcomes are persisted
- **Testability**: Components are isolated and independently testable
- **Scalability**: Cloud-friendly design for GitHub Codespaces and cloud deployment

---

## Core Modules

### 1. **API Module** (`src/findmy/api/`)
- **Purpose**: HTTP interface for trading operations
- **Key Components**:
  - `main.py`: FastAPI application factory
  - `app.py`: Route definitions and handlers
  - `schemas.py`: Pydantic request/response models
  - `common/`: Shared utilities (errors, middleware, enums)
  - `sot/`: Source of Truth API routes

**Responsibilities**:
- Accept Excel file uploads
- Validate input format and structure
- Route requests to execution engine
- Return execution results and error responses

**Constraints**:
- Does NOT contain trading logic
- Does NOT persist data directly (delegates to SOT)
- Does NOT calculate positions (delegates to execution)

---

### 2. **Execution Module** (`src/findmy/execution/`)
- **Purpose**: Paper and live trading execution engine
- **Current Implementation**: `paper_execution.py`

**Responsibilities**:
- Accept order intents (parsed from Excel)
- Simulate order fills based on price
- Track positions and P&L
- Return execution results

**Design**:
- Stateless: Takes input → produces output (no side effects)
- Deterministic: No randomness, same input = same result
- Pluggable: Can swap paper execution with live execution adapters

---

### 3. **Source of Truth (SOT) Module** (`services/sot/`)
- **Purpose**: Authoritative data store for all trading facts
- **Implementation**: SQLite with SQLAlchemy ORM

**Core Tables**:
- `order_requests`: Initial trading intent
- `orders`: Executed orders
- `order_events`: Append-only event log
- `order_fills`: Individual fill records
- `positions`: Current position state
- `trades`: Trade-level aggregations

**Key Principles**:
- **Append-only**: Facts are immutable; corrections are new records
- **Single source of truth**: All modules must read from SOT
- **Separation of concerns**: SOT only stores data, doesn't make decisions

---

### 4. **Strategy Module** (Future - `src/findmy/strategies/`)
- **Purpose**: Generate trading signals
- **Interface**: Input market data → Output order intents
- **Constraint**: Stateless and isolated from execution

---

### 5. **Trade Service (TS) Module** (`services/ts/`) ✅ – Phase 2

**Purpose**: Aggregate trades from SOT and provide P&L calculations, position tracking, and trade lifecycle management.

**Implementation Status**: ✅ Complete

**Architecture**:
```
TS API Layer (10 endpoints)
    ↓ reads from
TS Service Layer (business logic)
    ↓ accesses via
TS Repository Layer (queries)
    ↓ reads from
SOT (Order, OrderFill, OrderCost)
```

**Key Responsibilities**:
- Read-only integration with SOT
- Trade aggregation (entry order → exit order sequences)
- P&L calculations (gross, net, realized, unrealized, fees)
- Position inventory tracking with cost basis averaging
- Trade lifecycle (OPEN, PARTIAL, CLOSED)
- Performance metrics and analytics

**Database Models**:
- **Trade**: Entry/exit order pairs with status and strategy tracking
- **TradePnL**: P&L snapshots with fees and performance metrics
- **TradePosition**: Inventory state with average price and cumulative data
- **TradePerformance**: Time-bucketed metrics

**API Endpoints**:
```
POST   /api/v1/ts/trades/open           → Create new trade
POST   /api/v1/ts/trades/{id}/close     → Close/partial close
GET    /api/v1/ts/trades/{id}           → Get trade details
GET    /api/v1/ts/trades                → List trades
GET    /api/v1/ts/trades/{id}/pnl       → Get trade P&L
GET    /api/v1/ts/pnl/total             → Aggregate P&L
GET    /api/v1/ts/positions/{symbol}    → Get position inventory
GET    /api/v1/ts/positions             → List positions
GET    /api/v1/ts/health                → Health check
```

**Design Principles**:
- **Read-only SOT integration**: Never modifies SOT data
- **Layer separation**: API → Service → Repository → Models
- **Testable**: 14/14 tests passing with comprehensive coverage
- **Deterministic**: Same input always produces same P&L

---

```
User Upload (Excel)
    ↓
[API] Validate & Parse
    ↓
[Execution] Process Orders → Calculate Fills
    ↓
[SOT] Persist → Orders, Fills, Positions
    ↓
[API Response] Return Results & Summary
    ↓
[Future Analytics] Read from SOT → Generate Reports
```

---

## Design Principles

### 1. **Separation of Concerns**
- **API** ≠ **Execution** ≠ **Storage** ≠ **Strategy**
- Each module has a single, clear responsibility
- Modules communicate through well-defined contracts (schemas)

### 2. **Determinism**
- Paper execution is fully deterministic
- No external API calls during execution (price is deterministic)
- Enables replaying and debugging

### 3. **Auditability**
- Every order, fill, and position is persisted
- Complete audit trail from intent → execution → result
- Future compliance and regulatory reporting

### 4. **Immutability of Facts**
- Orders and fills cannot be modified, only appended
- Corrections are new records
- Enables historical replay and reconciliation

### 5. **Stateless Components**
- Execution engine is pure: `f(orders) → fills`
- Strategy logic is pure: `f(market_data) → signals`
- Simplifies testing and deployment

### 6. **Cloud-First Design**
- No local file dependencies
- Works in GitHub Codespaces, Docker, serverless
- Environment-agnostic

---

## Module Dependency Graph

```
┌──────────────┐
│   API        │  ← User-facing HTTP endpoints
└──────┬───────┘
       │ uses
       ↓
┌──────────────┐
│ Execution    │  ← Deterministic trading logic
└──────┬───────┘
       │ persists to
       ↓
┌──────────────┐
│    SOT       │  ← Append-only data store
└──────────────┘
       ↑
       │ reads from
┌──────┴───────┐
│ Analytics,   │
│ Reports,     │
│ Audit        │
└──────────────┘
```

**Key Rule**: Lower layers (SOT) don't depend on upper layers (API). Upper layers can depend on lower layers, but only through defined interfaces.

---

## Future Extensions

### Phase 2: Strategy Engine
- Add signal generation
- Integrate market data feeds
- Route signals → execution

### Phase 3: Risk Management
- Pre-trade risk checks
- Position limits
- Loss limits

### Phase 4: Backtesting
- Historical data replay
- Execution simulation
- Performance analysis

### Phase 5: Live Trading
- Broker/exchange adapters
- Real-time order management
- Reconciliation

### Phase 6: Analytics & Reporting
- P&L analysis
- Risk metrics
- Compliance reports
- Audit trails
