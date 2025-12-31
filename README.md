# FINDMY (FM) – Paper Trading Execution Engine

Small. Cute. Flexible. Funny Project

> **FINDMY (FM)** is a modular Python-based trading bot focused on research-first development, starting with a robust **paper trading execution engine** using Excel input and FastAPI.

**Latest Release:** v0.6.0 | **License:** MIT | **Status:** Active Development ⚡

---

## � Table of Contents

- [Quick Start](#quick-start)
- [Project Vision](#project-vision)
- [Latest Features (v0.6.0)](#latest-features-v060)
- [Previous Features (v0.5.0)](#previous-features-v050)
- [Current Features (v0.4.0)](#current-features-v040)
- [Quick Start (Installation & Setup)](#quick-start)
- [REST API Overview](#rest-api-fastapi)
- [Excel Input Format](#excel-input-format)
- [Repository Structure](#repository-structure)
- [Development](#development)
- [Security Features](#security-features)
- [Contributing](#contributing)
- [License & Disclaimer](#license)

---

## 📚 Full Documentation

**Need detailed information?** Check the full docs:

| Document | Purpose |
|----------|---------|
| **[docs/README.md](docs/README.md)** | 📍 **START HERE** – Documentation navigation hub |
| **[docs/api.md](docs/api.md)** | REST API endpoints with examples |
| **[docs/architecture.md](docs/architecture.md)** | System design and data flow |
| **[docs/configuration.md](docs/configuration.md)** | Environment setup & secrets |
| **[docs/execution.md](docs/execution.md)** | Execution engine details |
| **[docs/market-integration.md](docs/market-integration.md)** | Real-time market data |
| **[docs/risk-management.md](docs/risk-management.md)** | Risk management & pip sizing (v0.6.0) |
| **[docs/manual-approval.md](docs/manual-approval.md)** | Order approval workflow (v0.5.0) |
| **[docs/strategy.md](docs/strategy.md)** | Strategy development guide |
| **[docs/roadmap.md](docs/roadmap.md)** | Project roadmap & timeline |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | How to contribute |
| **[DOCUMENTATION.md](DOCUMENTATION.md)** | Documentation standards |

---

## 🚀 Project Vision

FINDMY is designed as a **production-grade trading system**, not a demo bot.

**Core Principles:**
- 🏗️ **Modular** – Strategy, execution, risk, and persistence are separate
- 🔬 **Research-First** – Paper trading & backtesting before live trading
- ☁️ **Cloud-Ready** – Runs on GitHub Codespaces (no local setup needed)
- 📊 **Observable** – SQL persistence for auditability and analysis
- 🔒 **Secure** – File validation, safe uploads, error isolation

---

## ✨ Latest Features (v0.6.0)

### 🎯 Pip-Based Order Sizing

✅ **Pip Multiplier System**
- Configure `pip_multiplier` (default 2.0) – 1 pip = multiplier × minQty
- Exchange-aware sizing using Binance LOT_SIZE (minQty, stepSize, maxQty)
- Automatic rounding to exchange step size
- `calculate_order_qty(symbol, pips=1)` function for conversion
- Example: `5 pips × 2.0 × 0.00001 = 0.0001 BTC`

✅ **Order Creation with Pips**
- Queue orders with `pips` field instead of fixed quantity
- Automatic conversion to proper qty with exchange validation
- Direct qty still supported (backward compatible)
- Both stored in pending order for audit trail

✅ **Exchange LOT_SIZE Integration**
- `get_exchange_info(symbol)` fetches Binance limits
- Automatic caching to avoid repeated API calls
- Graceful fallback with safe defaults
- Validates all calculated quantities against exchange limits

### 🛡️ Pre-Pending Risk Checks

✅ **Position Size Limits**
- Configurable max position as % of account equity
- Default: 10% max per position
- Prevents over-concentration risk
- Tracks current exposure and checks new orders against limit

✅ **Daily Loss Limits**
- Configurable max daily loss as % of equity
- Default: 5% max daily loss
- Tracks realized losses within trading day
- Prevents cascading losses

✅ **Risk Check Behavior**
- Risk checks run automatically before order queueing
- Violations DO NOT block orders (don't prevent approval queue)
- Violation details added as notes to pending order
- User can still approve risk-violating orders (with warning)
- Enables user judgment while tracking all violations

✅ **Risk API**
- `check_position_size(symbol, qty)` → passed/failed status
- `check_daily_loss()` → passed/failed status
- `check_all_risks(symbol, qty)` → (passed, [violations])
- All with optional db_session for testing

### 📊 Dashboard Risk Metrics Card

✅ **Real-Time Risk Display**
- Portfolio Exposure percentage vs 10% limit
- Daily Loss amount and percentage vs 5% limit
- Color-coded progress bars (green/yellow/red)
- Updates every 60 seconds via WebSocket
- Mobile-responsive design

✅ **Risk Visualization**
- Current exposure % with visual progress bar
- Daily loss tracking with dollar amount
- Threshold indicators (50%, 80%, 100%)
- Visual warnings as thresholds approached
- Clear status (safe/warning/critical)

### ⚙️ Pytest Timeout Control

✅ **Global Timeout Configuration**
- Default 30-second timeout for all tests
- Thread-based timeout method (prevents hangs)
- Configurable per-test with `@pytest.mark.timeout(N)`
- pytest-timeout v2.2.0 integration

✅ **Test Marking**
- Unit tests: 10-second timeout default
- Integration tests: 15-second timeout
- Slow/backtesting tests: 300-second timeout
- Custom timeouts supported per test

✅ **Timeout Safety**
- Tests hanging > 30s automatically fail
- Prevents CI/CD pipeline stalls
- Consistent timeout behavior across environments
- Clear timeout error messages

### 📚 Documentation

✅ **Risk Management Guide**
- Complete pip sizing examples and formulas
- Risk check workflows and API reference
- Configuration guide (env vars, config class)
- Dashboard metrics explanation
- Best practices and guidelines

✅ **Roadmap Updates**
- v0.6.0 completion status
- Features checklist (all ✓)
- Test results (19/19 ✓)
- Breaking changes: None

---

## ✨ Previous Features (v0.5.0)

### 🛡️ Manual Order Approval System (Safety Enhancement)

✅ **Mandatory Approval Queue**
- ALL orders (Excel, strategy, backtest) require user approval before execution
- No order bypasses the pending queue
- Prevents accidental execution and market manipulation
- Complete audit trail with timestamps and reviewers

✅ **Dashboard Integration**
- Visual "Pending Orders Queue" section on dashboard
- Real-time pending order count badge
- One-click approve/reject buttons
- Batch approval ready (future enhancement)
- WebSocket live updates

✅ **REST API Endpoints**
- `GET /api/pending` – List pending orders (with filters)
- `POST /api/pending/approve/{id}` – Approve order for execution
- `POST /api/pending/reject/{id}` – Reject order with reason
- Full programmatic control of approval workflow

✅ **Audit & Compliance**
- Source attribution (excel, strategy, backtest)
- Reviewer tracking (who approved/rejected)
- Timestamp tracking for all decisions
- Optional notes for reasoning
- Rejection reason capture

### 📊 Strategy Framework & Signal-to-Orders

✅ **Abstract Strategy Base**
- Simple interface for building custom strategies
- Market data and backtesting support
- Signal generation (buy/sell/hold)
- Confidence scoring (0-1 scale)

✅ **MovingAverageStrategy Implementation**
- 10/20/50-period EMA cross-over
- Example of full strategy lifecycle
- Automatic order generation
- Ready-to-extend template

✅ **Strategy Signal Processing**
- Signals converted to pending orders
- Strategy name and confidence tracked
- Signal evaluation with market context
- Confidence-based filtering

✅ **Strategy Backtesting**
- Run strategies on historical data
- Multi-symbol support
- Equity curve tracking
- Performance metrics (win rate, Sharpe ratio, max drawdown)

---

## ✨ Current Features (v0.4.0)


### 📊 Realtime Market Data Integration

✅ **Binance Public API**
- Real-time spot prices for all major pairs (BTC, ETH, SOL, etc.)
- No API key required – public data only
- 60-second cache TTL to avoid rate limiting
- Graceful fallback if Binance is unavailable
- Automatic retry with exponential backoff

✅ **Unrealized PnL Calculation**
- Live portfolio valuation with current market prices
- Unrealized gains/losses updated every 30 seconds
- Mark-to-market calculations
- Cost basis tracking with execution costs

✅ **WebSocket Live Updates**
- Real-time dashboard updates every 30 seconds
- No page reload needed
- Auto-reconnect on disconnect
- Fallback to polling if WebSocket unavailable

### 📈 Basic Backtesting

✅ **Backtest Engine**
- Historical OHLCV data from Binance
- Multi-symbol simulation support
- Configurable initial capital and timeframes
- Equity curve tracking
- Performance metrics calculation

✅ **Performance Metrics**
- Total return percentage
- Maximum drawdown
- Win rate
- Sharpe ratio (placeholder for enhancement)
- Trade-by-trade results

### 📋 Paper Trading Execution (v0.3.0+)

✅ **Advanced Order Processing**
- Partial fill support with configurable fill percentages
- Full-fill by default (backward compatible)
- Order types: MARKET, LIMIT, STOP_LOSS
- Duplicate detection (prevents double execution)
- BUY and SELL orders with position tracking
- Graceful error handling

✅ **Execution Costs Simulation**
- Configurable slippage (simulates adverse price movement)
- Transaction fees (maker & taker fees per order)
- Cost tracking per trade and aggregated
- Realistic price impact modeling

✅ **Stop-Loss Automation**
- Automatic stop-loss order triggers
- Price-based triggering with current price monitoring
- Complete trade execution with proper P&L calculation
- Status tracking (NEW → TRIGGERED → FILLED)

✅ **Enhanced Reporting**
- Detailed per-trade breakdown (qty, effective price, fees, slippage)
- Aggregated summary metrics (total fees, slippage, realized PnL)
- Position tracking with realized/unrealized P&L
- Trade history with cost basis and execution details

### 🌐 REST API (FastAPI)

✅ **Core Endpoints**
- `GET /health` – Health check
- `GET /` – Interactive HTML Dashboard
- `POST /paper-execution` – Execute orders from Excel

✅ **Position & Trade Endpoints**
- `GET /api/positions` – Current positions with unrealized PnL
- `GET /api/trades` – Trade history (JSON)
- `GET /api/summary` – Performance summary with market values

✅ **Realtime Updates**
- `WS /ws/dashboard` – WebSocket live updates (30s interval)
- `POST /api/backtest` – Run backtest simulation

✅ **Security**
- File type validation (MIME + extension)
- Size limits (10MB max)
- Safe filenames (UUID-based)
- Auto cleanup of temp files
- Input validation

✅ **Developer Experience**
- Beautiful Dashboard at `/` – Real-time TS & SOT monitoring
- Interactive Swagger UI at `/docs`
- ReDoc at `/redoc`
- Detailed error messages
- Full type hints

### 🧪 Testing & CI/CD

✅ **112+ Pytest Tests**
- Execution logic coverage
- API endpoint testing
- Excel parsing validation
- Market data & WebSocket tests
- Backtesting validation
- Error scenarios

✅ **GitHub Actions CI/CD**
- Tests on Python 3.10, 3.11, 3.12
- Code quality (black, ruff, mypy)

- Security scanning (Bandit, pip-audit)
- Coverage reporting

### 📦 Dependency Management

✅ **Split Dependencies**
- `requirements-prod.txt` – Production only
- `requirements-dev.txt` – Dev tools + testing
- Poetry support in `pyproject.toml`
- Vulnerability scanning

---

## 🚀 Quick Start

### Installation

```bash
# Clone repo
git clone https://github.com/KaisukaTran/findmy-fm.git
cd findmy-fm

# Create virtual environment
python3.10 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements-prod.txt
# OR for development:
pip install -r requirements-dev.txt
```

### Initialize Database

```bash
# Create tables and add test positions
python << 'EOF'
import sys
sys.path.insert(0, '/workspaces/findmy-fm')
from services.ts.db import engine, SessionLocal
from services.ts.models import Base, TradePosition

# Create tables
Base.metadata.create_all(bind=engine)

# Add test positions (optional)
db = SessionLocal()
positions = [
    TradePosition(symbol='BTC', quantity=0.5, avg_entry_price=45000, total_cost=22500),
    TradePosition(symbol='ETH', quantity=5.0, avg_entry_price=2000, total_cost=10000),
]
for pos in positions:
    db.add(pos)
db.commit()
db.close()

print('✅ Database initialized with test positions')
EOF
```

### Run the API

```bash
# Option 1: Uvicorn (recommended)
uvicorn src.findmy.api.main:app --host 0.0.0.0 --port 8000

# Option 2: Direct Python
python src/findmy/api/main.py
```

Server runs at: `http://localhost:8000`

### 📊 View the Dashboard (with Live Market Data)

Navigate to `http://localhost:8000/` to see the beautiful, responsive dashboard with **live Binance prices and mark-to-market valuation**:
- **System Status** – Database, Trade Service, and SOT health
- **Current Positions** – Symbols, quantities, average prices, total cost, **live market prices, market value, and unrealized P&L** *(NEW)*
- **Trade History** – Recent trades with P&L metrics
- **Summary Cards** – Total trades, realized/unrealized P&L, **total equity, total market value**, total invested *(NEW)*
- **Live Prices** – Real-time data from Binance (60-second cache, no API key needed)

The dashboard auto-refreshes every 30 seconds (prices) and supports mobile/tablet viewing.

[**📖 Dashboard Documentation**](docs/dashboard.md) | [**📖 Market Integration**](docs/market-integration.md)

### Try It Out

```bash
# 1. View dashboard with live prices
# Open: http://localhost:8000/

# 2. Execute paper trading
curl -X POST http://localhost:8000/paper-execution \
  -F "file=@examples/sample_purchase_order_with_header.xlsx"

# 3. View API docs
# Open: http://localhost:8000/docs
```

---

## 📁 Repository Structure

```
findmy-fm/
├─ src/findmy/
│  ├─ api/
│  │  ├─ main.py                 # FastAPI app (endpoints, file upload)
│  │  ├─ schemas.py              # Pydantic request/response models
│  │  ├─ sot/
│  │  │  ├─ routes.py            # SOT API endpoints
│  │  │  └─ schemas.py           # SOT schemas
│  │  └─ common/
│  │     ├─ errors.py            # Error handling
│  │     └─ middleware.py        # CORS, logging
│  ├─ services/
│  │  └─ market_data.py          # Binance price integration
│  ├─ strategies/
│  │  ├─ base.py                 # Strategy interface
│  │  └─ moving_average.py       # Example: EMA crossover strategy
│  └─ execution/
│     └─ paper_execution.py      # Execution engine (fully typed)
├─ services/
│  ├─ ts/                        # Trade Service (aggregates trades)
│  │  ├─ models.py               # Trade, Position models
│  │  ├─ db.py                   # SQLAlchemy session
│  │  └─ routes.py               # Trade API endpoints
│  ├─ sot/                       # Source of Truth (order/fill records)
│  │  ├─ pending_orders.py       # Pending order model (approval queue)
│  │  ├─ pending_orders_service.py # Pending order management
│  │  ├─ models.py               # Order, Fill models
│  │  ├─ db.py                   # SQLAlchemy session
│  │  └─ routes.py               # SOT API endpoints
│  ├─ risk/                      # Risk Management (v0.6.0)
│  │  ├─ pip_sizing.py           # Pip-based order sizing
│  │  ├─ risk_management.py      # Position/loss checks
│  │  └─ __init__.py             # Module exports
│  ├─ executor/                  # Order execution service
│  ├─ backtesting/               # Backtesting engine
│  ├─ report/                    # Performance reporting
│  └─ ai/                        # AI/signal generation
├─ tests/
│  ├─ test_api.py                # API endpoint tests
│  ├─ test_paper_execution.py    # Execution engine tests
│  ├─ test_risk_management.py    # Risk checks & pip sizing tests
│  ├─ test_market_data.py        # Market data tests
│  ├─ test_pending_orders.py     # Approval workflow tests
│  └─ test_*.py                  # Feature-specific tests
├─ examples/
│  ├─ README.md                  # Excel format guide
│  └─ sample_purchase_order_*.xlsx
├─ docs/
│  ├─ README.md                  # Documentation navigation hub ⭐
│  ├─ api.md                     # REST API reference
│  ├─ architecture.md            # System design
│  ├─ execution.md               # Execution engine details
│  ├─ market-integration.md      # Real-time market data
│  ├─ risk-management.md         # Risk management & pip sizing
│  ├─ manual-approval.md         # Order approval workflow
│  ├─ strategy.md                # Strategy development guide
│  ├─ roadmap.md                 # Project roadmap
│  ├─ configuration.md           # Environment setup
│  ├─ modules.md                 # Code organization
│  ├─ rules.md                   # Architectural rules
│  ├─ SOT.md                     # Data model
│  ├─ devlog/                    # Development journal
│  ├─ diagrams/                  # (Future) Architecture diagrams
│  └─ archive/                   # Historical docs (v0.2.0, v0.3.0)
├─ db/
│  ├─ migrations/                # (Future) Database migrations
│  └─ *.db                       # SQLite databases (generated)
├─ data/
│  ├─ uploads/                   # Temp uploaded files (auto-cleaned)
│  └─ sot/                       # SOT data exports
├─ .github/workflows/
│  └─ tests.yml                  # CI/CD pipeline
├─ scripts/
│  ├─ start_api.sh               # Run API server
│  └─ test_sot_dal.py            # Test utilities
├─ static/
│  └─ css/                       # Dashboard styles
├─ templates/
│  ├─ base.html                  # Dashboard base template
│  └─ dashboard.html             # Main dashboard UI
├─ conftest.py                   # Pytest configuration & fixtures
├─ pytest.ini                    # Pytest settings (timeout, markers)
├─ pyproject.toml                # Poetry, tools configuration
├─ requirements-prod.txt         # Production dependencies
├─ requirements-dev.txt          # Development tools & testing
├─ LICENSE                       # MIT License
├─ README.md                     # This file (project overview)
├─ CONTRIBUTING.md               # Contribution guide
├─ CHANGELOG.md                  # Release notes
└─ DOCUMENTATION.md              # Documentation standards
```

---

## 📊 Excel Input Format

**Sheet Name:** `purchase order` (required)

**With Headers (English):**
| Order ID | Quantity | Price | Trading Pair |
|---|---|---|---|
| ORD001 | 10.5 | 50000 | BTC/USD |

**Alternative English Headers:**
| Client ID | Quantity | Price | Symbol |
|---|---|---|---|
| ORD001 | 10.5 | 50000 | BTC/USD |

**Without Headers (Positional):**
- Column A: Client Order ID
- Column B: Quantity
- Column C: Price
- Column D: Symbol

See [examples/](examples/) for sample files.

---

## 🛠️ Development

### Run Tests

```bash
# All tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=src --cov-report=html

# Specific test
pytest tests/test_paper_execution.py::TestParseOrdersFromExcel -v
```

### Code Quality

```bash
# Format
black src/ tests/

# Lint
ruff check src/ tests/

# Type check
mypy src/ --ignore-missing-imports

# Security
bandit -r src/
pip-audit
```

---

## 🔐 Security Features

| Feature | Details |
|---------|---------|
| 🔒 **File Validation** | MIME type + extension check |
| 📏 **Size Limits** | 10MB maximum |
| 🆔 **Safe Filenames** | UUID-based (prevents collisions) |
| 🗑️ **Auto Cleanup** | Temp files deleted after use |
| ✅ **Input Validation** | Numeric type checking |
| 🔄 **Error Isolation** | Bad rows don't crash batch |
| 📝 **Type Safety** | 100% type hints on new code |
| 📚 **Documentation** | Comprehensive docstrings |

---

## 🗺️ Roadmap

### v0.2.0 (Next)
- [ ] SELL orders with position reduction
- [ ] Partial fills
- [ ] Order cancellation
- [ ] Enhanced reporting
- [ ] Database migrations

### v0.3.0
- [ ] Async processing
- [ ] WebSocket updates
- [ ] Trade history API
- [ ] P&L calculations
- [ ] Analytics

### v1.0.0
- [ ] Live trading
- [ ] Rate limiting
- [ ] Backtesting
- [ ] Strategy framework
- [ ] Risk management

See [docs/roadmap.md](docs/roadmap.md) for details.

---

## 🤝 Contributing

Contributions welcome! Please:

1. Read [CONTRIBUTING.md](CONTRIBUTING.md)
2. Check [Issues](https://github.com/KaisukaTran/findmy-fm/issues)
3. Fork & create feature branch
4. Run tests: `pytest tests/ -v`
5. Format code: `black src/ tests/`
6. Submit Pull Request

---

## 📄 License

MIT License – See [LICENSE](LICENSE) for details.

Open source and community-driven. 🎉

---

## ⚠️ Disclaimer

This project is for **research and educational purposes only**.

**Not financial advice.** Do not use for live trading without thorough testing and risk management.

---

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/KaisukaTran/findmy-fm/issues)
- **Discussions**: [GitHub Discussions](https://github.com/KaisukaTran/findmy-fm/discussions)
- **Docs**: [Full Documentation](docs/)

---

## 📊 Project Stats

- **Language**: Python 3.10+
- **Framework**: FastAPI + SQLAlchemy + pandas
- **Database**: SQLite
- **Tests**: 40+ unit & integration tests
- **Coverage**: >80%
- **Type Coverage**: 100% on new code
- **Lines of Code**: ~2000 (core + tests)

---

## 🙏 Acknowledgments

Built with ❤️ using:
- [FastAPI](https://fastapi.tiangolo.com/) – Modern Python web framework
- [SQLAlchemy](https://www.sqlalchemy.org/) – SQL toolkit
- [pandas](https://pandas.pydata.org/) – Data analysis
- [pytest](https://pytest.org/) – Testing

---

**Happy trading! 🚀**

> *"Build the system as if it will trade real money — even when it doesn't."*

*Last updated: January 2025*
