# Order History Service (SOT)

## 1. Purpose

The Order History Service (SOT – Source of Truth) is the authoritative system of record for **all trading orders and executions** within the FINDMY platform.

Any trading-related fact that is not recorded in SOT is considered **non-existent** by the system.

SOT is designed to support:
- Auditability
- Debugging and incident analysis
- Accurate PnL calculation
- Historical replay
- Long-term system evolution

---

## 2. Scope of Responsibility

The SOT module is responsible for storing and exposing:

- Order intents (order requests)
- Executed orders sent to exchanges
- Order lifecycle states and events
- Fill-level execution details
- Transaction fees and costs
- Realized and unrealized PnL (snapshot-based)
- Decision context at the time of trade
- Risk validation results
- Reconciliation state between local system and exchange

The SOT module is **not responsible** for:
- Strategy logic
- Signal generation
- Risk evaluation logic
- Position management logic
- PnL calculation algorithms (only results are stored)

---

## 3. Core Design Principles

### 3.1 Single Source of Truth
All downstream modules (executor, analytics, reporting, audit) must rely exclusively on data stored in SOT.  
No module is allowed to maintain its own version of order state.

---

### 3.2 Immutable Facts
Certain data represents historical facts and must never be modified once written:
- Orders
- Order fills
- Order events

Corrections must be represented as **new records**, never as in-place updates.

---

### 3.3 Derived Facts Are Isolated
Derived data such as:
- Aggregated fees
- Cost basis
- PnL snapshots

are stored separately from immutable facts and may be recalculated if required.

---

### 3.4 Explainability First
Every executed order must be traceable back to:
- The original intent (order request)
- The market context at decision time
- The indicators and signals used
- The outcome of risk validation

This principle enables post-trade analysis and regulatory-grade audits.

---

## 4. Core Domain Concepts

### 4.1 Order Request
Represents an **intent to trade**, originating from:
- Spreadsheet input
- API request
- Manual operation

An order request may be rejected and never produce an actual order.

---

### 4.2 Order
Represents a **real order** submitted to an exchange.

The order entity is the central reference point in SOT and links to:
- Execution events
- Fills
- Fees
- PnL snapshots
- Reconciliation state

---

### 4.3 Order Fill
An order may be executed in multiple partial fills.

Each fill records:
- Execution price
- Executed quantity
- Fee amount and asset
- Liquidity type (maker / taker)

Fill-level data is the **only reliable source** for accurate PnL calculation.

---

### 4.4 Order Event
Order events capture lifecycle transitions such as:
- SENT
- ACKNOWLEDGED
- PARTIALLY_FILLED
- FILLED
- CANCELED
- ERROR

Event records support replay, debugging, and incident investigation.

---

### 4.5 Decision Context
Decision context captures **why** a trade occurred, including:
- Technical indicators (e.g. RSI, EMA, VWAP)
- Market snapshot (price, spread, order book state)
- Signal strength or confidence score

Without decision context, automated trading behavior cannot be explained.

---

### 4.6 Fees and PnL
Fees and PnL values are stored as **snapshots**:
- They reflect the state at a specific calculation time
- They may be recomputed if pricing models change

PnL data is not considered immutable historical fact.

---

### 4.7 Risk Validation and Reconciliation
- Risk validation records explain why an order request was accepted or rejected
- Reconciliation records compare local order state with exchange-reported state

These records are critical for operational safety.

---

## 5. Data Lifecycle Overview

Order data flows through the following lifecycle:

Order Request  
→ Risk Validation  
→ Order Creation  
→ Order Events  
→ Order Fills  
→ Fee Aggregation  
→ PnL Snapshot  
→ Reconciliation

---

## 6. Future-Oriented Design

The SOT schema is intentionally designed to support:
- Multiple exchanges
- Partial fills and complex execution paths
- Position-based aggregation
- Historical replay
- Post-trade analytics and compliance auditing

The schema may evolve, but core design principles must remain unchanged.
