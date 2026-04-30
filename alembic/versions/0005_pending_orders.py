"""pending_orders table (manual approval workflow)

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, side TEXT NOT NULL,
            quantity REAL NOT NULL, price REAL NOT NULL,
            order_type TEXT NOT NULL DEFAULT 'MARKET',
            pips REAL,
            source TEXT NOT NULL, source_ref TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at DATETIME, reviewed_by TEXT,
            note TEXT, strategy_name TEXT, confidence REAL,
            live_order_id TEXT,
            CHECK (side IN ('BUY','SELL')),
            CHECK (order_type IN ('MARKET','LIMIT','STOP_LOSS')),
            CHECK (status IN ('pending','approved','rejected')),
            CHECK (quantity > 0), CHECK (price >= 0)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_status ON pending_orders(status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_created_at ON pending_orders(created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_source ON pending_orders(source)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pending_orders")
