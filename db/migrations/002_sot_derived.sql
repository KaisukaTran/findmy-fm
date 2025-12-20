-- ==================================================
-- Migration: 002_sot_derived
-- Module: Order History Service (SOT)
-- Description: Derived, audit, and explainability tables
-- Created at: 2025-12-20
-- ==================================================
-- =========================================
-- Table: order_costs
-- Purpose: Aggregated trading costs (derived)
-- =========================================

CREATE TABLE order_costs (
    order_id INTEGER PRIMARY KEY,

    total_fee REAL,
    fee_asset TEXT,
    commission_rate REAL,

    FOREIGN KEY(order_id)
        REFERENCES orders(id)
);
-- =========================================
-- Table: order_pnl
-- Purpose: PnL snapshot per order (derived)
-- =========================================

CREATE TABLE order_pnl (
    order_id INTEGER PRIMARY KEY,

    realized_pnl REAL,
    unrealized_pnl REAL,
    cost_basis REAL,

    calculated_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(order_id)
        REFERENCES orders(id)
);
-- =========================================
-- Table: order_decision_context
-- Purpose: Explain why a trade decision was made
-- =========================================

CREATE TABLE order_decision_context (
    order_request_id INTEGER PRIMARY KEY,

    indicators TEXT,
    -- JSON snapshot: RSI, EMA, VWAP...

    signal_strength REAL,

    market_snapshot TEXT,
    -- JSON: price, spread, orderbook summary

    FOREIGN KEY(order_request_id)
        REFERENCES order_requests(id)
);
-- =========================================
-- Table: order_risk_checks
-- Purpose: Risk validation results
-- =========================================

CREATE TABLE order_risk_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_request_id INTEGER NOT NULL,

    rule_code TEXT,
    passed INTEGER,
    message TEXT,

    checked_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(order_request_id)
        REFERENCES order_requests(id)
);
-- =========================================
-- Table: exchange_reconciliation
-- Purpose: Local vs exchange state comparison
-- =========================================

CREATE TABLE exchange_reconciliation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_id INTEGER NOT NULL,

    local_status TEXT,
    exchange_status TEXT,

    mismatch INTEGER,
    checked_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(order_id)
        REFERENCES orders(id)
);
-- =========================================
-- Indexes
-- =========================================

CREATE INDEX idx_order_costs_order_id
    ON order_costs(order_id);

CREATE INDEX idx_order_pnl_order_id
    ON order_pnl(order_id);

CREATE INDEX idx_decision_context_request_id
    ON order_decision_context(order_request_id);

CREATE INDEX idx_risk_checks_request_id
    ON order_risk_checks(order_request_id);

CREATE INDEX idx_reconciliation_order_id
    ON exchange_reconciliation(order_id);
