"""live_orders_audit table for tracking every live exchange call

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS live_orders_audit (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            pending_order_id INTEGER NOT NULL,
            symbol        TEXT NOT NULL,
            side          TEXT NOT NULL,
            quantity      REAL NOT NULL,
            dry_run       INTEGER NOT NULL DEFAULT 1,
            exchange_request  TEXT,
            exchange_response TEXT,
            status        TEXT NOT NULL DEFAULT 'sent',
            error_message TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_order_id ON live_orders_audit(pending_order_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_created_at ON live_orders_audit(created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS live_orders_audit")
