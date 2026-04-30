"""trade service tables (trades, trade_pnl, trade_positions, trade_performance)

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_order_id INTEGER NOT NULL,
            exit_order_id INTEGER,
            symbol TEXT NOT NULL, side TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            entry_qty REAL NOT NULL, entry_price REAL NOT NULL, entry_time DATETIME NOT NULL,
            exit_qty REAL, exit_price REAL, exit_time DATETIME,
            current_qty REAL NOT NULL, current_price REAL,
            strategy_code TEXT, signal_source TEXT, requested_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            effective_price REAL, fees REAL DEFAULT 0.0, slippage_amount REAL DEFAULT 0.0
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS trade_pnl (
            trade_id INTEGER PRIMARY KEY,
            gross_pnl REAL NOT NULL DEFAULT 0.0,
            total_fees REAL NOT NULL DEFAULT 0.0,
            entry_fees REAL NOT NULL DEFAULT 0.0,
            exit_fees REAL NOT NULL DEFAULT 0.0,
            net_pnl REAL NOT NULL DEFAULT 0.0,
            return_pct REAL NOT NULL DEFAULT 0.0,
            cost_basis REAL NOT NULL DEFAULT 0.0,
            realized_pnl REAL NOT NULL DEFAULT 0.0,
            unrealized_pnl REAL NOT NULL DEFAULT 0.0,
            max_profit REAL, max_loss REAL, max_drawdown REAL,
            duration_minutes INTEGER,
            calculated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(trade_id) REFERENCES trades(id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS trade_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL, avg_entry_price REAL NOT NULL,
            total_traded REAL NOT NULL DEFAULT 0.0,
            total_cost REAL NOT NULL DEFAULT 0.0,
            strategy_code TEXT,
            last_trade_time DATETIME,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS trade_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_time DATETIME NOT NULL, bucket_type TEXT NOT NULL,
            total_trades INTEGER NOT NULL DEFAULT 0,
            winning_trades INTEGER NOT NULL DEFAULT 0,
            losing_trades INTEGER NOT NULL DEFAULT 0,
            breakeven_trades INTEGER NOT NULL DEFAULT 0,
            total_pnl REAL NOT NULL DEFAULT 0.0,
            net_pnl REAL NOT NULL DEFAULT 0.0,
            total_fees REAL NOT NULL DEFAULT 0.0,
            win_rate REAL NOT NULL DEFAULT 0.0,
            avg_pnl REAL NOT NULL DEFAULT 0.0,
            avg_win REAL, avg_loss REAL,
            max_consecutive_wins INTEGER, max_consecutive_losses INTEGER,
            calculated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_trade_positions_symbol ON trade_positions(symbol)")


def downgrade() -> None:
    for t in ("trade_performance", "trade_positions", "trade_pnl", "trades"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
