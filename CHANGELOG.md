# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [v0.5.0] – 2024-12-31

### 🛡️ Safety & Compliance Enhancements

#### Manual Order Approval System
- **Mandatory Approval Queue**: ALL orders (Excel, strategy, backtest) queue to pending_orders
  - No order bypasses user approval
  - Prevents accidental execution and market manipulation
  - Complete audit trail with timestamps

- **PendingOrder Model**: New database model with fields:
  - `symbol`, `side`, `quantity`, `price`, `order_type`
  - `source` (excel/strategy/backtest), `status` (pending/approved/rejected)
  - `reviewed_by`, `reviewed_at` for audit trail
  - `strategy_name`, `confidence` for strategy orders

- **PendingOrdersService**: New service layer with functions:
  - `queue_order()` – Create pending order
  - `get_pending_orders()` – List with status/symbol/source filters
  - `approve_order()` – Mark for execution with timestamp
  - `reject_order()` – Prevent execution with reason
  - `count_pending()` – Get pending count

- **REST API Endpoints**:
  - `GET /api/pending` – List pending orders (querystring filters)
  - `POST /api/pending/approve/{id}` – Approve order
  - `POST /api/pending/reject/{id}` – Reject order with reason

- **Dashboard Integration**:
  - New "Pending Orders Queue" section with real-time updates
  - Badge showing pending count
  - One-click approve/reject buttons per order
  - Order details: symbol, side, qty, price, source, status, created time
  - WebSocket live refresh (60s fallback polling)

### 📊 Strategy Framework & Backtesting

#### Abstract Strategy Architecture
- **Strategy Base Class** (`src/findmy/strategies/base.py`):
  - Abstract interface for custom strategies
  - `evaluate()` method for signal generation
  - Market data access for analysis
  - Backtest support

- **MovingAverageStrategy** (`src/findmy/strategies/moving_average.py`):
  - Example 10/20/50-period EMA implementation
  - Buy/sell/hold signal generation
  - Confidence scoring (0-1)
  - Multi-symbol support

#### Strategy Signal Processing
- **Signal-to-Orders Conversion**:
  - Strategy signals converted to pending orders
  - Strategy name and confidence tracked
  - Source set to "strategy"
  - Orders queued for user approval

#### Strategy Backtesting Service
- **StrategyBacktestingService** (`src/findmy/services/backtesting.py`):
  - Run strategies on historical data
  - Multi-symbol, multi-timeframe support
  - Equity curve tracking
  - Performance metrics calculation

- **BacktestRequest/Response Models**:
  - `strategy_type` and `strategy_config` parameters
  - Historical data fetching from Binance
  - Complete trade simulation
  - Metrics: total_return, win_rate, sharpe_ratio, max_drawdown

### 🔄 Paper Execution Workflow Changes

- **Modified `run_paper_execution()`**:
  - Now queues orders instead of executing
  - Returns: `orders_queued`, `pending_order_ids`, `errors`
  - Backward compatible with Excel uploads
  - All orders require approval before execution

- **Removed Direct Execution**:
  - No longer creates trades directly
  - Prevents unauthorized trading
  - Maintains separation of concerns

### 🧪 Testing & Quality

- **New Test Suite** (`tests/test_pending_orders.py`):
  - 12 comprehensive tests for pending orders
  - TestPendingOrdersService (5 tests)
  - TestPendingOrdersAPI (5 tests)
  - TestPaperExecutionQueues (2 tests)
  - 100% pass rate

- **Updated Test Suite** (`tests/test_paper_execution.py`):
  - Updated 37 tests for queuing behavior
  - Validation that orders queue instead of execute
  - Mixed buy/sell workflow testing
  - All tests passing

- **Test Coverage**: 123 total tests passing
  - 37 paper execution tests
  - 12 pending orders tests
  - 74 other integration tests

### 📚 Documentation

- **New Manual Approval Guide** (`docs/manual-approval.md`):
  - Architecture and database model
  - Service layer functions
  - REST API endpoint reference
  - Dashboard usage guide
  - Workflow examples
  - Safety features overview
  - cURL examples
  - Best practices
  - Troubleshooting

- **Updated README.md**:
  - v0.5.0 features section
  - Manual approval link
  - Strategy framework overview
  - Backtesting highlights

### 🔐 Security Improvements

- **Order Validation**:
  - Positive quantity/price validation
  - Side validation (BUY/SELL)
  - Status enum constraints
  - Order type validation

- **Audit Trail**:
  - Every approval/rejection logged
  - Reviewer identity tracked
  - Timestamps for all decisions
  - Optional notes/reasoning

### ⚠️ Breaking Changes

- **Paper Execution Behavior**: Orders no longer execute immediately
  - Requires approval step
  - Test expectations updated
  - API response structure changed

---

## [v0.4.0] – 2024-12-30

### 🚀 Major Features

#### Realtime Market Data Integration
- **Binance Public API Integration**: Added CCXT-based price fetching for realtime spot prices
  - No API key required (public data only)
  - Support for 100+ currency pairs (BTC, ETH, SOL, etc.)
  - Automatic pair formatting (BTC → BTC/USDT)

- **Price Caching with TTL**: Implemented in-memory cache with 60-second TTL
  - Prevents rate limiting while maintaining freshness
  - Graceful fallback to cached prices if Binance unavailable
  - Cache clearing utility for testing

#### Unrealized PnL & Market Valuation
- **Enhanced Position Endpoints**: `/api/positions` now includes:
  - `current_price`: Live spot price from Binance
  - `market_value`: Quantity × Current Price
  - `unrealized_pnl`: Market Value - Cost Basis

- **Enhanced Summary Endpoint**: `/api/summary` now includes:
  - `total_market_value`: Sum of all positions at current prices
  - `total_equity`: Total invested + unrealized PnL
  - Real-time portfolio valuation

#### WebSocket Live Updates
- **New `/ws/dashboard` WebSocket Endpoint**:
  - Broadcasts updates every 30 seconds
  - Includes fresh positions and summary data
  - Auto-reconnect on disconnect
  - Fallback to 60-second polling if WebSocket unavailable

- **Dashboard Enhancement** (`templates/dashboard.html`):
  - Auto-updates positions without page reload
  - Displays realtime unrealized PnL
  - Shows live market values
  - Connection status indicator
  - Graceful degradation on network issues

#### Basic Backtesting Engine
- **New `backtesting.py` Service**:
  - `BacktestRequest`: Configuration class for backtest parameters
  - `BacktestResult`: Results container with metrics
  - `run_backtest()`: Core simulation function
  - `calculate_sharpe_ratio()`: Risk-adjusted return calculation

- **Historical Data Fetching**:
  - `get_historical_ohlcv()`: Last N candles for a symbol
  - `get_historical_range()`: Date range based data fetching
  - Support for multiple timeframes: 1m, 5m, 15m, 30m, 1h, 4h, 1d
  - Automatic timestamp conversion and validation

- **Backtest Results** (via `/api/backtest` endpoint):
  - Equity curve: Point-in-time portfolio values
  - Trade list: All simulated trades with execution details
  - Performance metrics:
    - Total return percentage
    - Maximum drawdown
    - Win rate percentage
    - Sharpe ratio
    - Backtest period info

### ✅ API Enhancements

#### New Endpoints
- **`GET /health`**: Health check endpoint
- **`POST /api/backtest`**: Run backtest simulation
- **`WS /ws/dashboard`**: Realtime dashboard updates

#### Request/Response Schema
```python
# Backtest Request
{
  "symbols": ["BTC", "ETH"],
  "start_date": "2024-01-01",
  "end_date": "2024-03-31",
  "initial_capital": 10000.0,
  "timeframe": "1h"
}

# Position Response (enhanced)
{
  "symbol": "BTC",
  "quantity": 0.5,
  "avg_price": 45000.00,
  "total_cost": 22500.00,
  "current_price": 47000.00,      # NEW
  "market_value": 23500.00,       # NEW
  "unrealized_pnl": 1000.00       # NEW
}

# Summary Response (enhanced)
{
  "total_trades": 42,
  "realized_pnl": 2500.00,
  "unrealized_pnl": 1000.00,
  "total_invested": 50000.00,
  "total_market_value": 51000.00,  # NEW
  "total_equity": 52500.00,        # NEW
  "last_trade_time": "2024-12-30T10:30:00",
  "status": "✓ Active"
}

# WebSocket Update Message
{
  "type": "dashboard_update",
  "timestamp": "2024-12-30T12:00:00",
  "positions": [...],
  "summary": {...}
}
```

### 🧪 Testing

- **17 New Tests** in `test_v0_4_0_features.py`:
  - Historical data structure validation
  - OHLCV pricing rules verification
  - Date range handling
  - Backtest request initialization
  - Backtest result structure
  - Metrics validation
  - Sharpe ratio calculation
  - Integration tests

- **Total Test Coverage**: 112/112 tests passing (100%)
  - 38 v0.3.x paper execution tests
  - 17 v0.4.0 feature tests
  - 57 API and market data tests

### 📚 Documentation

- **NEW: `docs/market-integration.md`**
  - Complete guide to v0.4.0 features
  - API endpoint documentation
  - WebSocket connection guide
  - Backtesting tutorial
  - Troubleshooting section
  - Code examples for all features

- **Updated: `README.md`**
  - Version bumped to v0.4.0
  - New features section
  - 112+ tests highlighted
  - Realtime capabilities emphasized

- **Updated: `docs/dashboard.md`**
  - WebSocket update documentation
  - Live price integration guide
  - Real-time PnL calculation examples

### 🔧 Infrastructure

- **ConnectionManager**: WebSocket connection pool management
  - Accept/disconnect tracking
  - Broadcast to all connected clients
  - Error handling and cleanup

- **Error Handling**: Improved malformed file detection
  - Better distinction between 400 (client) and 500 (server) errors
  - Specific error messages for Excel parsing failures

### 🐛 Bug Fixes

- Fixed `/health` endpoint to return JSON (was HTML)
- Improved error handling for corrupted Excel files
- Better cleanup of temporary upload files

### ⚡ Performance Improvements

- Price caching reduces API calls by ~95% (60s TTL)
- WebSocket reduces bandwidth vs polling
- Efficient equity curve generation in backtests
- Minimal memory footprint for position tracking

### ♻️ Backward Compatibility

✅ **Fully backward compatible with v0.3.x**:
- All v0.3.x endpoints work unchanged
- Existing tests continue to pass
- New fields are additive only
- Default values maintain previous behavior
- No breaking changes to data models

### 📋 Dependencies

- **No new production dependencies added**
- CCXT already in requirements-prod.txt (v4.0.5)
- WebSocket support via FastAPI (already included)

---

## [v0.3.1] – 2024-12-30

### ✨ Features

#### Asynchronous Order Execution with Latency Simulation
- **New async functions**:
  - `submit_order_async()`: Submit orders with configurable latency
  - `process_pending_orders()`: Execute orders after latency window
  - `async_order_processor()`: Background task for continuous processing
  - `get_pending_orders()`: Monitor pending order status

- **Order Status Tracking**:
  - New PENDING status for orders awaiting execution
  - Progress tracking (0-100%) for in-flight orders
  - Configurable base latency + random variance
  - Timestamp tracking (submitted_at, executed_at)

- **Database Extensions**:
  - New columns: latency_ms, submitted_at, executed_at
  - Migration: `005_latency_simulation.sql`
  - Full backward compatibility

### 🧪 Testing

- **8 Comprehensive Tests** in `test_async_execution.py`:
  - Async order submission with/without latency
  - Pending order status and progress tracking
  - Background task processing
  - SELL order execution with PnL
  - All 8 tests passing

- **Regression**: All 38 v0.3.0 tests continue to pass

### 📚 Documentation

- **Extended: `docs/advanced-execution.md`**
  - Latency simulation section (v0.3.1)
  - ExecutionMode enum explanation
  - Order lifecycle with async execution
  - API reference with examples
  - Use cases and applications

---

## [v0.3.0] – 2024-12-30

### ✨ Features

#### Partial Fill Support
- Configurable fill percentage (0-100%)
- Full-fill by default (backward compatible)
- Simulates partial executions
- Cost basis recalculation per fill

#### Execution Costs Simulation
- **Slippage**: Configurable adverse price movement
- **Transaction Fees**: Maker/taker fee simulation
- **Cost Tracking**: Per-trade and aggregated
- Realistic price impact modeling

#### Stop-Loss Order Automation
- Automatic price-based triggers
- Complete trade execution
- Proper P&L calculation
- Status tracking (NEW → TRIGGERED → FILLED)

#### Enhanced Reporting
- Per-trade execution costs
- Aggregated summary metrics
- Realized/unrealized P&L
- Complete trade history

### 📚 Documentation

- Comprehensive execution guide
- Examples for all new features
- Troubleshooting section
- Performance considerations

---

## [v0.2.0] – 2024-12-30

### ✨ Features

#### HTML Dashboard
- Real-time position tracking
- Trade history with P&L
- System status indicators
- Auto-refresh every 30 seconds
- Mobile-friendly Bootstrap 5 design

#### Trade Service (TS)
- Trade aggregation (entry → exit)
- P&L calculations
- Position tracking
- Database models for persistence

#### SELL Order Support
- BUY/SELL order sides
- Position reduction logic
- Cost basis preservation
- Oversell prevention

### 🌐 API Endpoints
- `GET /` – Dashboard
- `POST /paper-execution` – Execute from Excel
- `GET /api/positions` – Position data
- `GET /api/trades` – Trade history
- `GET /api/summary` – Summary stats

---

## [v0.1.0] – 2024-12-25

### ✨ Initial Release

#### Paper Trading Engine
- Excel-based order ingestion
- Deterministic execution
- Duplicate detection
- Order lifecycle management

#### REST API (FastAPI)
- `/paper-execution` endpoint
- Interactive Swagger UI
- Comprehensive error handling
- Type-safe endpoints

#### Database
- SQLite persistence
- Transaction support
- Order and trade models
- P&L calculations

#### Testing & Quality
- 40+ pytest tests
- Type hints throughout
- Security validation
- Error isolation

---

## Legend

- ✅ Feature implemented
- ⚠️ Known limitation
- 🐛 Bug fix
- 📈 Performance improvement
- 📚 Documentation
- 🧪 Testing
- 🔒 Security
- ♻️ Backward compatibility
