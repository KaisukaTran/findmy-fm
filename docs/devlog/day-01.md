# FINDMY (FM) – Development Log (Day 01)

**Date:** Day 01
**Author:** Kai
**Environment:** GitHub Codespaces (VS Code Web on iPad)

---

## 🎯 Day 01 Objectives

* Set up development environment **without needing a personal computer**
* Initialize FINDMY (FM) project
* Build **paper trading execution engine (v1)**
* Create **FastAPI backend** for Excel upload and trigger execution
* Standardize documentation & GitHub workflow

---

## 🧱 1. Environment Setup (GitHub Codespaces)

### 1.1 Create Repository

* Create GitHub repo: `findmy-fm`
* Skip default README (will standardize later)

### 1.2 Open Codespaces

* GitHub → Repo → **Code → Codespaces → Create codespace**
* VS Code Web opens directly in browser (iPad)

---

## 🐍 2. Python Environment Setup

### 2.1 Create Virtual Environment

```bash
python -m venv .venv
source .venv/bin/activate
```

> `.venv` is used to isolate dependencies for the project

### 2.2 Install Required Packages

```bash
pip install pandas sqlalchemy openpyxl fastapi uvicorn python-multipart
pip freeze > requirements.txt
```

---

## 📁 3. Initial Project Structure

```bash
mkdir -p src/findmy/{api,execution}
mkdir -p data/uploads
mkdir -p scripts

touch src/findmy/__init__.py
```

Main structure:

```
findmy-fm/
├─ src/findmy/
│  ├─ api/
│  └─ execution/
├─ data/
├─ scripts/
```

---

## 📦 4. Fix Python Import Convention (`src/` layout)

### 4.1 Error Encountered

```
ModuleNotFoundError: No module named 'findmy'
```

### 4.2 Solution (Production Standard)

Create `pyproject.toml` file:

```toml
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "findmy"
version = "0.1.0"
requires-python = ">=3.10"

[tool.setuptools.packages.find]
where = ["src"]
```

Install in editable mode:

```bash
pip install -e .
```

---

## 📊 5. Paper Trading Execution Engine (v1)

### 5.1 Objectives

* Read Excel file
* Sheet name: `purchase order`
* Support Excel formats:

  * With header
  * Without header
  * Mismatched header → fallback to A,B,C,D
* BUY only
* Immediate full-fill
* Save Orders / Trades / Positions to SQLite

### 5.2 Main File

```
src/findmy/execution/paper_execution.py
```

### 5.3 Important Errors & Fixes

#### ❌ `'int' object has no attribute 'lower'`

* Root cause: Excel has no header
* Fix: detect `df.columns` is `int` → positional mapping

#### ❌ `missing required columns`

* Root cause: Excel header doesn't match
* Fix: fallback to positional mapping

#### ❌ `return outside function`

* Root cause: incorrect indentation when pasting code
* Fix: replace entire function, don't patch line by line

---

## 🌐 6. FastAPI Backend

### 6.1 Create FastAPI App

File:

```
src/findmy/api/main.py
```

Health check:

```http
GET /
```

Paper execution:

```http
POST /paper-execution
```

---

### 6.2 Run Server

```bash
PYTHONPATH=src uvicorn findmy.api.main:app --host 0.0.0.0 --port 8000 --reload
```

Swagger UI:

```
/docs
```

---

## 🔁 7. FastAPI Startup Script

Create file:

```
scripts/start_api.sh
```

Content:

```bash
#!/bin/bash
source .venv/bin/activate
export PYTHONPATH=src
uvicorn findmy.api.main:app --host 0.0.0.0 --port 8000 --reload
```

Make executable:

```bash
chmod +x scripts/start_api.sh
```

Run:

```bash
./scripts/start_api.sh
```

---

## 🧪 8. End-to-End Test

* Open `/docs`
* Upload Excel file
* Execute
* Receive JSON:

  * orders
  * trades
  * positions

---

## 🧾 9. Git Workflow for the Day

```bash
git status
git add .
git commit -m "feat: paper trading execution + fastapi upload"
git push
```

---

## 📚 10. Documentation

* Standardize `README.md`
* Create `docs/` structure
* Devlog by day (`docs/devlog/day-01.md`)

---

## 🧠 Notes for Tomorrow (Day 02)

* Start from `paper_execution.py`
* Add PnL & equity curve
* Design Strategy interface
* Don't touch FastAPI unless necessary

---

> *Day 01 focused on foundation: environment, execution, API, and documentation. The system is ready for strategy development.*
