"""
Migration 006: Add pending_orders table for manual order approval workflow.

This migration introduces the pending orders queue, where ALL orders must be
manually approved by the user before execution to increase safety.

Features:
- Pending orders table with approval workflow
- Status tracking: pending, approved, rejected
- Source tracking: excel, strategy, backtest
- User approval metadata
- Optional rejection notes
"""

import sqlite3
from datetime import datetime
from pathlib import Path


def migrate_up(db_path: str):
    """Create pending_orders table."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Create pending_orders table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            
            -- Order details (mirror of Order model)
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            order_type TEXT NOT NULL DEFAULT 'MARKET',
            
            -- Source tracking
            source TEXT NOT NULL,
            source_ref TEXT,
            
            -- Approval workflow
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at DATETIME,
            reviewed_by TEXT,
            
            -- Optional metadata
            note TEXT,
            strategy_name TEXT,
            confidence REAL,
            
            -- Indexes for common queries
            CHECK (side IN ('BUY', 'SELL')),
            CHECK (order_type IN ('MARKET', 'LIMIT', 'STOP_LOSS')),
            CHECK (status IN ('pending', 'approved', 'rejected')),
            CHECK (quantity > 0),
            CHECK (price >= 0)
        )
    """)
    
    # Create indexes for common queries
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_pending_orders_status 
        ON pending_orders(status)
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_pending_orders_created_at 
        ON pending_orders(created_at DESC)
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_pending_orders_source 
        ON pending_orders(source)
    """)
    
    conn.commit()
    conn.close()
    print(f"✅ Migration 006 up: Created pending_orders table")


def migrate_down(db_path: str):
    """Drop pending_orders table."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("DROP TABLE IF EXISTS pending_orders")
    
    conn.commit()
    conn.close()
    print(f"✅ Migration 006 down: Dropped pending_orders table")


if __name__ == "__main__":
    # Run migration
    db_path = Path(__file__).parent.parent.parent / "data" / "findmy_fm_paper.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "down":
        migrate_down(str(db_path))
    else:
        migrate_up(str(db_path))
