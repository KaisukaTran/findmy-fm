"""system_state key-value table (for distributed flags like EMERGENCY_HALT)

Revision ID: 0009
Revises: 0008
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS system_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    op.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('emergency_halt', '0')")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS system_state")
