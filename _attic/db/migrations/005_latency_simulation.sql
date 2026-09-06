-- Migration 005: Add latency simulation support
-- Adds timing columns for async order execution tracking

-- Add latency_ms column to track simulated execution delay
ALTER TABLE orders ADD COLUMN latency_ms INTEGER DEFAULT 0;

-- Add submitted_at column to track when order was submitted for async execution
ALTER TABLE orders ADD COLUMN submitted_at DATETIME NULL;

-- Add executed_at column to track when async order was actually executed
ALTER TABLE orders ADD COLUMN executed_at DATETIME NULL;

-- Note: Status column now includes PENDING state for orders awaiting execution
-- Future statuses: NEW, PENDING, PARTIALLY_FILLED, FILLED, CANCELLED, TRIGGERED
