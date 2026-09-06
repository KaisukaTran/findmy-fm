-- ==================================================
-- Migration: 001_sot_core
-- Module: Order History Service (SOT)
-- Description: Core tables for order lifecycle
-- Created at: 2025-12-20
-- ==================================================
-- =========================================
-- Table: order_requests
-- Purpose: Store trading intents (input)
-- =========================================

CREATE TABLE order_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    source TEXT NOT NULL,
    source_ref TEXT,

    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,

    quantity REAL NOT NULL,
    price REAL,

    strategy_code TEXT,
    requested_by TEXT,
    requested_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    raw_payload TEXT
);
-- =========================================
-- Table: orders
-- Purpose: Actual orders sent to exchange
-- =========================================

CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_request_id INTEGER NOT NULL,

    exchange TEXT NOT NULL,
    exchange_order_id TEXT,
    client_order_id TEXT,

    position_id TEXT,

    status TEXT NOT NULL,
    time_in_force TEXT,

    reduce_only INTEGER DEFAULT 0,
    post_only INTEGER DEFAULT 0,

    sent_at DATETIME,
    filled_at DATETIME,

    avg_price REAL,
    executed_qty REAL,

    error_message TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(order_request_id)
        REFERENCES order_requests(id)
);
-- =========================================
-- Table: order_events
-- Purpose: Order lifecycle events
-- =========================================

CREATE TABLE order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_id INTEGER NOT NULL,

    event_type TEXT NOT NULL,
    event_time DATETIME DEFAULT CURRENT_TIMESTAMP,

    payload TEXT,

    FOREIGN KEY(order_id)
        REFERENCES orders(id)
);
-- =========================================
-- Table: order_fills
-- Purpose: Fill-level execution details
-- =========================================

CREATE TABLE order_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_id INTEGER NOT NULL,

    fill_price REAL NOT NULL,
    fill_qty REAL NOT NULL,

    fee_amount REAL,
    fee_asset TEXT,

    liquidity TEXT,
    filled_at DATETIME,

    FOREIGN KEY(order_id)
        REFERENCES orders(id)
);
-- =========================================
-- Indexes
-- =========================================

CREATE INDEX idx_order_requests_symbol
    ON order_requests(symbol);

CREATE INDEX idx_orders_request_id
    ON orders(order_request_id);

CREATE INDEX idx_orders_status
    ON orders(status);

CREATE INDEX idx_order_events_order_id
    ON order_events(order_id);

CREATE INDEX idx_order_fills_order_id
    ON order_fills(order_id);
