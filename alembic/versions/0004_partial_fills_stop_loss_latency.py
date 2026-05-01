"""add partial fills, stop loss, latency columns to orders/trades

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Migration 003: partial fills / costs
    for stmt in [
        "ALTER TABLE orders ADD COLUMN remaining_qty NUMERIC DEFAULT 0.0",
        "ALTER TABLE orders ADD COLUMN maker_fee_rate NUMERIC DEFAULT 0.001",
        "ALTER TABLE orders ADD COLUMN taker_fee_rate NUMERIC DEFAULT 0.001",
    ]:
        try:
            op.execute(stmt)
        except Exception:
            pass  # column already exists on existing DBs

    # Migration 004: stop-loss
    for stmt in [
        "ALTER TABLE orders ADD COLUMN order_type VARCHAR DEFAULT 'MARKET'",
        "ALTER TABLE orders ADD COLUMN stop_price NUMERIC",
    ]:
        try:
            op.execute(stmt)
        except Exception:
            pass

    # Migration 005: latency simulation
    for stmt in [
        "ALTER TABLE orders ADD COLUMN latency_ms INTEGER DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN submitted_at DATETIME",
        "ALTER TABLE orders ADD COLUMN executed_at DATETIME",
    ]:
        try:
            op.execute(stmt)
        except Exception:
            pass


def downgrade() -> None:
    pass  # SQLite cannot DROP COLUMN – acceptable for a demo project
