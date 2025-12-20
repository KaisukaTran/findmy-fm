# SOT Database Build Plan

## Objective

This document describes the step-by-step plan for implementing the SOT database schema in a controlled, auditable manner.

Each phase results in a commit-ready output.

---

## Phase 0 — Design Freeze

**Goal:** Lock architecture before implementation

Tasks:
- Review and approve `docs/sot.md`
- Review and approve SOT ERD v1
- Freeze schema version as `sot_schema_v1`

Deliverables:
- Approved documentation
- No database code

---

## Phase 1 — Core Schema Implementation

**Commit:** `sot-schema-core`

Tables:
- `order_requests`
- `orders`
- `order_events`
- `order_fills`

Tasks:
- Define primary and foreign keys
- Define mandatory constraints
- Add essential indexes:
  - order_request_id
  - order_id
  - symbol
  - status
  - exchange_order_id

Deliverables:
- SQL migration file (SQLite)
- Schema validation queries

---

## Phase 2 — Derived and Audit Tables

**Commit:** `sot-schema-derived`

Tables:
- `order_costs`
- `order_pnl`
- `order_decision_context`
- `order_risk_checks`
- `exchange_reconciliation`

Tasks:
- Define relationships to core tables
- Mark derived vs immutable tables explicitly in comments

Deliverables:
- Second migration SQL file
- Schema documentation update

---

## Phase 3 — Data Access Layer (DAL)

**Commit:** `sot-dal-v1`

Tasks:
- Implement repository methods:
  - create_order_request
  - create_order
  - append_order_event
  - insert_order_fill
  - save_fee_aggregation
  - save_pnl_snapshot
- Ensure transactional integrity
- Enforce immutability rules at DAL level

Deliverables:
- DAL module
- Unit tests for core workflows

---

## Phase 4 — Validation and Seed Data

**Commit:** `sot-seed-and-test`

Tasks:
- Create representative seed data:
  - One order request
  - One executed order
  - Multiple partial fills
  - Fee aggregation
  - PnL snapshot
- Validate:
  - Full lifecycle traceability
  - Fill-based fee correctness
  - Referential integrity

Deliverables:
- Seed scripts
- Validation queries
- Test results

---

## Phase 5 — Integration Readiness

**Commit:** `sot-ready-for-integration`

Tasks:
- Verify executor can operate using SOT exclusively
- Confirm no bypass paths exist
- Document integration expectations for other modules

Deliverables:
- Integration notes
- Final review checklist

---

## Success Criteria

- All order-related facts are traceable end-to-end
- No ambiguity between intent, execution, and outcome
- PnL can be recalculated without data loss
- Audit and replay are possible without external logs
