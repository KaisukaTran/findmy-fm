-- Migration 003: Add Partial Fill Support
-- Date: 2025-12-30
-- Description: Add columns to support partial order fills

-- Add remaining_qty to orders table to track unfilled quantity
ALTER TABLE orders ADD COLUMN remaining_qty NUMERIC DEFAULT NULL;

-- Add fill_price and fees to trades table to track actual execution details
ALTER TABLE trades ADD COLUMN effective_price NUMERIC DEFAULT NULL;
ALTER TABLE trades ADD COLUMN fees NUMERIC DEFAULT 0.0;
ALTER TABLE trades ADD COLUMN slippage_amount NUMERIC DEFAULT 0.0;

-- Update existing trades to have effective_price = price if NULL
UPDATE trades SET effective_price = price WHERE effective_price IS NULL;
