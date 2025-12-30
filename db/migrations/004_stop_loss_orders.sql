-- Migration 004: Add stop-loss order support
-- Adds order_type and stop_price columns to orders table

-- Add order_type column if it doesn't exist
ALTER TABLE orders ADD COLUMN order_type VARCHAR DEFAULT 'MARKET';

-- Add stop_price column for stop-loss orders
ALTER TABLE orders ADD COLUMN stop_price NUMERIC NULL;

-- Update status column constraint to include TRIGGERED state
-- (This is a documentation note; SQLite doesn't enforce CHECKs on existing columns)
-- Future statuses: NEW, PARTIALLY_FILLED, FILLED, CANCELLED, TRIGGERED
