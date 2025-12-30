# FINDMY (FM) – Paper Trading Execution Engine

Small. Cute. Flexible. Funny Project

> **FINDMY (FM)** is a modular Python-based trading bot focused on research-first development, starting with a robust **paper trading execution engine** using Excel input and FastAPI.

**Latest Release:** v0.4.0 | **License:** MIT | **Status:** Active Development ⚡

---

## 📚 Quick Links

**New to FINDMY?** Start here:
- **[Quick Start Guide](#quick-start)** – Get running in 5 minutes
- **[Full Documentation](docs/README.md)** – Complete guide
- **[API Reference](docs/api.md)** – REST endpoints with examples
- **[Configuration & Secrets](docs/configuration.md)** – Environment setup & security
- **[Database Schema](docs/database-schema.md)** – Data model
- **[Architecture](docs/architecture.md)** – System design
- **[Contributing](CONTRIBUTING.md)** – How to contribute

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

[**📖 Full Dashboard Documentation**](docs/dashboard.md) | [**📖 Market Data Integration**](docs/market-data.md)

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
│  │  ├─ main.py                 # FastAPI app (secure upload)
│  │  ├─ schemas.py              # Pydantic models
│  │  └─ common/
│  │     ├─ errors.py            # Error handling
│  │     └─ middleware.py        # Middleware
│  └─ execution/
│     └─ paper_execution.py      # Execution engine (fully typed)
├─ tests/
│  ├─ test_paper_execution.py    # 40+ tests
│  └─ test_api.py                # API tests
├─ examples/
│  ├─ README.md                  # Excel format guide
│  ├─ sample_purchase_order_with_header.xlsx
│  ├─ sample_purchase_order_english.xlsx
│  ├─ sample_purchase_order_no_header.xlsx
│  └─ sample_purchase_order_with_errors.xlsx
├─ docs/
│  ├─ api.md                     # REST API reference
│  ├─ database-schema.md         # Data model
│  ├─ architecture.md            # System design
│  ├─ execution.md               # Execution engine
│  └─ roadmap.md                 # Feature roadmap
├─ .github/workflows/
│  └─ tests.yml                  # CI/CD pipeline
├─ data/
│  ├─ uploads/                   # Temp files (auto-cleaned)
│  └─ findmy_fm_paper.db         # SQLite database
├─ requirements-prod.txt         # Production dependencies
├─ requirements-dev.txt          # Development tools
├─ pyproject.toml                # Poetry + tool config
├─ LICENSE                       # MIT License
├─ CONTRIBUTING.md               # Contribution guide
└─ README.md
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
