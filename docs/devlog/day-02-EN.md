# FINDMY (FM) – Development Log (Day 02)

**Date:** Day 02
**Author:** Kai
**Environment:** GitHub Codespaces (VS Code Web on iPad)

---

## 🎯 Day 02 Objectives (UPDATED)

Day 02 is **NOT focused on PnL**.
The focus is **building data foundation (Database & Auditability)** for FINDMY.

### Main Objectives

* Design **clear DB schema** for paper trading
* Save **all executed orders**
* Record **full history of requests from Excel (sheet `purchase order`)**
* Ensure **audit trail**: ability to trace back *who – when – uploaded which file – generated which orders*

> PnL & equity curve **for Day 03**

---

## 🧠 Scope Control

### ✅ Will Do

* Order history (orders table)
* Trade history (trades table)
* Position snapshot (positions table)
* **Request history** from Excel uploads
* Execution run tracking

### ❌ Not Yet

* PnL / equity
* Strategy logic
* Live trading
* Async execution

---

## 🗄️ 1. Database Design (Core of Day 02)

### 1.1 `execution_runs` Table

> Each Excel upload = **1 execution run**

```sql
execution_runs
----------------------
id (PK)
run_id (UUID)
source_file_name
sheet_name
created_at
notes
```

Meaning:

* `run_id`: links all orders/trades from 1 run
* `source_file_name`: name of uploaded Excel file
* `sheet_name`: default `purchase order`

---

### 1.2 `order_requests` Table (EXCEL HISTORY)

> Save **raw data read from Excel** (before execution)

```sql
order_requests
----------------------
id (PK)
run_id (FK)
row_index
client_order_id
qty
price
symbol
raw_data (JSON)
created_at
```

Meaning:

* Record **each row in purchase order sheet**
* `raw_data`: save original row for audit/debug

---

### 1.3 `orders` Table

```sql
orders
----------------------
id (PK)
run_id (FK)
client_order_id
symbol
side
qty
price
status
created_at
```

---

### 1.4 `trades` Table

```sql
trades
----------------------
id (PK)
order_id (FK)
symbol
side
qty
price
ts
```

---

### 1.5 `positions` Table

```sql
positions
----------------------
id (PK)
symbol
size
avg_price
updated_at
```

---

## 🔄 2. Data Flow (Day 02)

```text
Excel Upload
   ↓
Create execution_run
   ↓
Parse sheet "purchase order"
   ↓
Insert order_requests (raw history)
   ↓
Create orders
   ↓
Simulate trades
   ↓
Update positions
```

---

## 🔧 3. Task Breakdown (Implementation for the Day)

### 3.1 Persistence Layer

* [ ] Add `ExecutionRun` model
* [ ] Add `OrderRequest` model
* [ ] Attach `run_id` to orders & trades

### 3.2 Execution Layer

* [ ] Generate `run_id` for each execution
* [ ] Save order_requests before execution
* [ ] Execution **independent of API**

### 3.3 API Layer

* [ ] `/paper-execution` returns `run_id`
* [ ] New endpoints:

  * `GET /runs` (list execution runs)
  * `GET /runs/{run_id}` (details of 1 run)

---

## 🧪 4. Test Plan

### Test Case 1

* Upload 1 Excel file
* Expect:

  * 1 execution_run
  * N order_requests
  * N orders

### Test Case 2

* Upload same file 2 times
* Expect:

  * 2 different execution_runs
  * Data not overwritten

---

## 🧠 5. Design Decisions (VERY IMPORTANT)

* Excel ingestion **always saved**, even if execution fails
* DB is **single source of truth**
* API only triggers & queries, no state kept
* Strategy (Day 03) will **read from DB**, not Excel

---

## 📝 6. Anticipated Commands

```bash
# start api
./scripts/start_api.sh

# test upload
curl -X POST http://localhost:8000/paper-execution \
  -F "file=@data/orders_v1.xlsx"

# inspect db
sqlite3 data/findmy_fm_paper.db
```

---

## 🔮 7. Notes for Day 03

* Use existing DB to calculate PnL
* Strategy engine only generates signals
* No longer read Excel directly

---

> *Day 02 establishes foundation for auditability and ability to analyze full trading history.*
