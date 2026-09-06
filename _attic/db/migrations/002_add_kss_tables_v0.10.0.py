"""
Migration: Add KSS tables (v0.10.0)

Creates tables for KSS (Kai Strategy Service):
- kss_sessions: Pyramid DCA sessions
- kss_waves: Individual waves within sessions

Run with: python db/migrations/002_add_kss_tables_v0.10.0.py
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text, inspect
from services.sot.db import DATABASE_URL, engine


def check_table_exists(connection, table_name: str) -> bool:
    """Check if table exists in database."""
    inspector = inspect(connection)
    return table_name in inspector.get_table_names()


def run_migration():
    """Run the KSS tables migration."""
    
    print("=" * 60)
    print("Migration: Add KSS tables (v0.10.0)")
    print("=" * 60)
    
    with engine.connect() as conn:
        
        # Check if tables already exist
        if check_table_exists(conn, "kss_sessions"):
            print("✓ kss_sessions table already exists, skipping")
        else:
            print("Creating kss_sessions table...")
            conn.execute(text("""
                CREATE TABLE kss_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_type VARCHAR NOT NULL DEFAULT 'pyramid',
                    symbol VARCHAR NOT NULL,
                    entry_price FLOAT NOT NULL,
                    distance_pct FLOAT NOT NULL,
                    max_waves INTEGER NOT NULL,
                    isolated_fund FLOAT NOT NULL,
                    tp_pct FLOAT NOT NULL,
                    timeout_x_min FLOAT NOT NULL,
                    gap_y_min FLOAT NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'pending',
                    current_wave INTEGER NOT NULL DEFAULT 0,
                    avg_price FLOAT NOT NULL DEFAULT 0.0,
                    total_filled_qty FLOAT NOT NULL DEFAULT 0.0,
                    total_cost FLOAT NOT NULL DEFAULT 0.0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at DATETIME,
                    last_fill_at DATETIME,
                    completed_at DATETIME,
                    created_by VARCHAR,
                    note TEXT
                )
            """))
            print("✓ kss_sessions table created")
            
            # Create indexes
            print("Creating indexes for kss_sessions...")
            conn.execute(text("CREATE INDEX ix_kss_sessions_symbol ON kss_sessions(symbol)"))
            conn.execute(text("CREATE INDEX ix_kss_sessions_status ON kss_sessions(status)"))
            conn.execute(text("CREATE INDEX ix_kss_sessions_created_at ON kss_sessions(created_at)"))
            print("✓ kss_sessions indexes created")
        
        if check_table_exists(conn, "kss_waves"):
            print("✓ kss_waves table already exists, skipping")
        else:
            print("Creating kss_waves table...")
            conn.execute(text("""
                CREATE TABLE kss_waves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    wave_num INTEGER NOT NULL,
                    quantity FLOAT NOT NULL,
                    target_price FLOAT NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'pending',
                    filled_qty FLOAT,
                    filled_price FLOAT,
                    filled_at DATETIME,
                    pending_order_id INTEGER,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at DATETIME,
                    FOREIGN KEY (session_id) REFERENCES kss_sessions(id) ON DELETE CASCADE
                )
            """))
            print("✓ kss_waves table created")
            
            # Create indexes
            print("Creating indexes for kss_waves...")
            conn.execute(text("CREATE INDEX ix_kss_waves_session_id ON kss_waves(session_id)"))
            conn.execute(text("CREATE INDEX ix_kss_waves_status ON kss_waves(status)"))
            conn.execute(text("CREATE INDEX ix_kss_waves_pending_order_id ON kss_waves(pending_order_id)"))
            print("✓ kss_waves indexes created")
        
        conn.commit()
    
    print()
    print("=" * 60)
    print("Migration completed successfully!")
    print("=" * 60)


if __name__ == "__main__":
    run_migration()
