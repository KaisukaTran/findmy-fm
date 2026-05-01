"""Remove CHECK constraints on pending_orders that conflict with SQLAlchemy Enum storage.

SQLAlchemy Column(Enum(PendingOrderStatus)) stores enum names ('PENDING', 'APPROVED',
'REJECTED') but the original SQL migration used lowercase values in CHECK constraints.
Recreate table without CHECK constraints; validation is handled at the ORM layer.

Revision ID: 0011
Revises: 0010
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite cannot DROP CONSTRAINT — recreate the table without CHECK clauses.
    op.execute("""
        CREATE TABLE IF NOT EXISTS pending_orders_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            order_type TEXT NOT NULL DEFAULT 'MARKET',
            pips REAL,
            source TEXT NOT NULL,
            source_ref TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at DATETIME,
            reviewed_by TEXT,
            note TEXT,
            strategy_name TEXT,
            confidence REAL,
            live_order_id TEXT
        )
    """)
    op.execute("""
        INSERT INTO pending_orders_new
        SELECT id, symbol, side, quantity, price, order_type, pips,
               source, source_ref,
               UPPER(status),
               created_at, reviewed_at, reviewed_by,
               note, strategy_name, confidence, live_order_id
        FROM pending_orders
    """)
    op.execute("DROP TABLE pending_orders")
    op.execute("ALTER TABLE pending_orders_new RENAME TO pending_orders")
    op.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_status ON pending_orders(status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_created_at ON pending_orders(created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_pending_orders_source ON pending_orders(source)")


def downgrade() -> None:
    pass  # Not reversible without data loss risk
