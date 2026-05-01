"""Add ai_trusted column to pending_orders and create ai_decision_log + ai_consultants tables.

Revision ID: 0012
Revises: 0011
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add ai_trusted flag to pending_orders
    op.execute("ALTER TABLE pending_orders ADD COLUMN ai_trusted INTEGER NOT NULL DEFAULT 0")

    # AI decision audit log
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai_decision_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol            TEXT NOT NULL,
            signal            TEXT NOT NULL,
            confidence        REAL NOT NULL,
            reasoning         TEXT,
            action            TEXT NOT NULL,
            pending_order_id  INTEGER,
            consultant_votes  TEXT,
            market_context    TEXT,
            created_at        TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_log_symbol ON ai_decision_log(symbol)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ai_log_created ON ai_decision_log(created_at)")

    # AI consultant registry
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai_consultants (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            type        TEXT NOT NULL DEFAULT 'llm',
            config_json TEXT NOT NULL DEFAULT '{}',
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # AI agent state (running/stopped, paper performance)
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai_agent_state (
            key   TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Seed initial state
    op.execute("INSERT OR IGNORE INTO ai_agent_state(key,value) VALUES ('running','false')")
    op.execute("INSERT OR IGNORE INTO ai_agent_state(key,value) VALUES ('mode','paper')")
    op.execute("INSERT OR IGNORE INTO ai_agent_state(key,value) VALUES ('paper_start_date','')")


def downgrade() -> None:
    pass  # SQLite: cannot drop columns; tables can be dropped manually if needed
