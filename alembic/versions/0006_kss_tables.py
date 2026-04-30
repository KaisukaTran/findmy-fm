"""KSS pyramid strategy tables

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS kss_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_type TEXT NOT NULL DEFAULT 'pyramid',
            symbol TEXT NOT NULL,
            entry_price REAL NOT NULL, distance_pct REAL NOT NULL,
            max_waves INTEGER NOT NULL, isolated_fund REAL NOT NULL,
            tp_pct REAL NOT NULL, timeout_x_min REAL NOT NULL, gap_y_min REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            current_wave INTEGER NOT NULL DEFAULT 0,
            avg_price REAL NOT NULL DEFAULT 0.0,
            total_filled_qty REAL NOT NULL DEFAULT 0.0,
            total_cost REAL NOT NULL DEFAULT 0.0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME, last_fill_at DATETIME, completed_at DATETIME,
            created_by TEXT, note TEXT
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS kss_waves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            wave_num INTEGER NOT NULL,
            quantity REAL NOT NULL, target_price REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            filled_qty REAL, filled_price REAL, filled_at DATETIME,
            pending_order_id INTEGER,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at DATETIME,
            FOREIGN KEY(session_id) REFERENCES kss_sessions(id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_kss_sessions_symbol ON kss_sessions(symbol)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_kss_sessions_status ON kss_sessions(status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_kss_sessions_created_at ON kss_sessions(created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_kss_waves_session_id ON kss_waves(session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_kss_waves_status ON kss_waves(status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_kss_waves_pending_order_id ON kss_waves(pending_order_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS kss_waves")
    op.execute("DROP TABLE IF EXISTS kss_sessions")
