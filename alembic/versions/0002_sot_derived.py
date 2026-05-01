"""sot derived tables

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS order_costs (
            order_id INTEGER PRIMARY KEY,
            total_fee REAL, fee_asset TEXT, commission_rate REAL,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS order_pnl (
            order_id INTEGER PRIMARY KEY,
            realized_pnl REAL, unrealized_pnl REAL, cost_basis REAL,
            calculated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS order_decision_context (
            order_request_id INTEGER PRIMARY KEY,
            indicators TEXT, signal_strength REAL, market_snapshot TEXT,
            FOREIGN KEY(order_request_id) REFERENCES order_requests(id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS order_risk_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_request_id INTEGER NOT NULL,
            rule_code TEXT, passed INTEGER, message TEXT,
            checked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(order_request_id) REFERENCES order_requests(id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS exchange_reconciliation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            local_status TEXT, exchange_status TEXT,
            mismatch INTEGER, checked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_order_costs_order_id ON order_costs(order_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_order_pnl_order_id ON order_pnl(order_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_decision_context_request_id ON order_decision_context(order_request_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_risk_checks_request_id ON order_risk_checks(order_request_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_reconciliation_order_id ON exchange_reconciliation(order_id)")


def downgrade() -> None:
    for t in ("exchange_reconciliation", "order_risk_checks", "order_decision_context", "order_pnl", "order_costs"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
