# FINDMY – Documentation Structure

This document defines the **official documentation layout** for the FINDMY (FM) project.
It is designed to support **long-term development**, clear knowledge retention, and easy onboarding.

---

## 📁 Recommended `docs/` Structure

```
docs/
├─ README.md                # Entry point for documentation
├─ architecture.md          # System architecture & design decisions
├─ execution.md             # Execution engine details (paper & live)
├─ strategy.md              # Strategy interface & examples
├─ api.md                   # FastAPI endpoints & contracts
├─ devlog/
│  ├─ day-01.md             # Development log – Day 1
│  ├─ day-02.md             # Development log – Day 2
│  └─ day-xx.md             # Future days
└─ diagrams/
   ├─ architecture.png
   └─ execution-flow.png
```

---

## 📘 Purpose of Each Document

### `README.md` (inside docs/)

* High-level documentation index
* Links to all other documents
* Entry point for contributors

---

### `architecture.md`

* High-level system architecture
* Module responsibilities
* Data flow (strategy → execution → persistence)
* Design decisions & rationale

---

### `execution.md`

* Paper trading execution logic
* Order lifecycle
* Fill simulation
* Database schema overview
* Roadmap to live execution

---

### `strategy.md`

* Strategy interface definition
* Signal structure
* Example strategies
* Best practices (stateless, no look-ahead)

---

### `api.md`

* FastAPI endpoints
* Request / response examples
* Error handling
* Future async execution design

---

### `devlog/day-xx.md`

* Daily development journal
* Tracks:

  * What was done
  * Issues encountered
  * Fixes applied
  * Technical decisions
  * Next steps

This replaces long commit messages and preserves project memory.

---

## 📝 Template: `docs/devlog/day-02.md`

```md
# FINDMY – Development Log (Day 2)

## Objectives
-

## Work Completed
-

## Issues & Fixes
-

## Technical Decisions
-

## Lessons Learned
-

## Next Steps
-
```

---

## 📝 Template: `docs/architecture.md`

```md
# FINDMY – System Architecture

## Overview

## Core Modules
- Strategy
- Execution
- Risk
- Persistence

## Data Flow

## Design Principles

## Future Extensions
```

---

## 📝 Template: `docs/execution.md`

```md
# FINDMY – Execution Engine

## Paper Trading (v1)

## Order Lifecycle

## Database Model

## Known Limitations

## Roadmap to Live Execution
```

---

## 🔗 How This Fits with Root README

* Root `README.md` → **Project overview & quick start**
* `docs/README.md` → **Detailed technical documentation**
* `docs/devlog/` → **Project memory (Lâu Đài Ký Ức)**

---

## ✅ Recommended First Commits

```bash
git add docs/
git commit -m "docs: add documentation structure and templates"
```

---

> *Documentation is part of the system, not an afterthought.*
