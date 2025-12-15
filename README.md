# findmy-fm
Small. Cute. Flexible. Funny Project
# FINDMY (FM)

> **FINDMY (FM)** is a modular Python-based trading bot focused on research-first development, starting with a robust **paper trading execution engine** using Excel input and FastAPI.

---

## 🚀 Project Vision

FINDMY is designed as a **production-grade trading system**, not a demo bot.
The core principles are:

* Modular architecture (strategy, execution, risk, persistence)
* Research-first (paper trading & backtesting before live trading)
* Cloud-friendly development (GitHub Codespaces)
* Strong observability & auditability (SQL persistence)

---

## 🧱 Current Features (v1 – Implemented)

### ✅ Paper Trading Execution Engine

* Excel-based order ingestion
* Supports Excel:

  * With header
  * Without header
  * Header mismatch (fallback to positional A–D)
* Sheet name: `purchase order`
* Immediate full-fill simulation (BUY only – v1)
* SQLite persistence:

  * Orders
  * Trades
  * Positions

### ✅ FastAPI Backend

* REST API to upload Excel and trigger execution
* Swagger UI available out of the box
* Health check endpoint

### ✅ Cloud Development Setup

* Runs entirely on **GitHub Codespaces**
* No local machine required
* AI-assisted development using Copilot / Continue.dev

---

## 📁 Repository Structure

```
findmy-fm/
├─ src/
│  └─ findmy/
│     ├─ api/
│     │  └─ main.py              # FastAPI application
│     ├─ execution/
│     │  └─ paper_execution.py   # Paper trading engine
│     └─ __init__.py
├─ data/
│  ├─ uploads/                   # Uploaded Excel files
│  └─ findmy_fm_paper.db         # SQLite paper trading database
├─ scripts/
│  └─ start_api.sh               # Start FastAPI server
├─ .venv/
├─ requirements.txt
├─ pyproject.toml
└─ README.md
```

---

## 📊 Excel Input Specification

**Sheet name (required):**

```
purchase order
```

**Column order (A–D):**

| Column | Description                      |
| ------ | -------------------------------- |
| A      | Order sequence / client order id |
| B      | Buy quantity                     |
| C      | Order price                      |
| D      | Trading pair (symbol)            |

> Header row is optional. If headers do not match expected names, the system falls back to positional mapping.

---

## 🌐 API Endpoints

### Health Check

```
GET /
```

Response:

```json
{
  "status": "ok",
  "service": "FINDMY FM API"
}
```

---

### Paper Trading Execution

```
POST /paper-execution
```

**Description:**

* Upload Excel file
* Trigger paper trading execution
* Persist results to SQLite
* Return execution summary

**Example Response:**

```json
{
  "status": "success",
  "result": {
    "orders": 5,
    "trades": 5,
    "positions": [
      {
        "symbol": "BTC/USDT",
        "size": 0.3,
        "avg_price": 63500
      }
    ]
  }
}
```

---

## ▶️ How to Run (Development)

### 1️⃣ Activate Virtual Environment

```bash
source .venv/bin/activate
```

### 2️⃣ Start FastAPI Server

```bash
./scripts/start_api.sh
```

### 3️⃣ Open Swagger UI

```
/docs
```

---

## 🧠 Design Principles

* **Execution is deterministic**: same input → same result
* **Strategies are stateless** and isolated from execution
* **Persistence-first**: every action is auditable
* **Separation of concerns**: API ≠ execution ≠ strategy

---

## 🛣️ Roadmap

### v2

* PnL & equity curve calculation
* Detailed execution report (orders, trades)
* SELL orders support

### v3

* Strategy engine (signal → execution)
* Execution adapter pattern
* Slippage & latency simulation

### v4

* Async execution with execution_id
* Backtesting & replay engine

### v5

* Live trading adapters (exchange/broker)

---

## ⚠️ Disclaimer

This project is for **research and educational purposes only**.
It is **not financial advice** and should not be used for live trading without thorough testing and risk management.

---

## 👤 Author

**Kai**
Project: FINDMY (FM)

---

> *“Build the system as if it will trade real money — even when it doesn’t.”*
