-- Migration 007: users table for real authentication
-- Run after 006_pending_orders.py

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT    NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role       TEXT    NOT NULL DEFAULT 'trader',  -- 'admin' | 'trader'
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
