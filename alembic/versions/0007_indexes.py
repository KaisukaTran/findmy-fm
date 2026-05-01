"""performance indexes (v0.7.0)

Revision ID: 0007
Revises: 0006
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for stmt in [
        "CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)",
        "CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time)",
        "CREATE INDEX IF NOT EXISTS idx_trade_pnl_trade_id ON trade_pnl(trade_id)",
        "CREATE INDEX IF NOT EXISTS idx_trade_perf_bucket ON trade_performance(bucket_time, bucket_type)",
        "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)",
    ]:
        try:
            op.execute(stmt)
        except Exception:
            pass


def downgrade() -> None:
    for idx in ("idx_trades_entry_time", "idx_trades_exit_time", "idx_trade_pnl_trade_id",
                "idx_trade_perf_bucket", "idx_orders_created_at"):
        try:
            op.execute(f"DROP INDEX IF EXISTS {idx}")
        except Exception:
            pass
