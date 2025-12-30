-- Migration: Add columns for partial fills and execution costs
-- Adds remaining_qty to orders and effective_price, fees, slippage_amount to trades

PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;

-- Orders: add remaining_qty, maker_fee_rate, taker_fee_rate
ALTER TABLE orders ADD COLUMN remaining_qty NUMERIC DEFAULT 0.0;
ALTER TABLE orders ADD COLUMN maker_fee_rate NUMERIC DEFAULT 0.001;
ALTER TABLE orders ADD COLUMN taker_fee_rate NUMERIC DEFAULT 0.001;

-- Trades: add effective_price, fees, slippage_amount
ALTER TABLE trades ADD COLUMN effective_price NUMERIC;
ALTER TABLE trades ADD COLUMN fees NUMERIC DEFAULT 0.0;
ALTER TABLE trades ADD COLUMN slippage_amount NUMERIC DEFAULT 0.0;

COMMIT;
PRAGMA foreign_keys=ON;
