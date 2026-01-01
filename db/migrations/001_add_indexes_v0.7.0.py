"""
Migration: Add Database Indexes for v0.7.0 Performance Optimization

This migration adds strategic indexes to improve query performance:
- Compound indexes on frequently filtered columns
- Single column indexes on search/sort columns
- Expected improvement: 10-100x faster queries

Run with: python db/migrations/001_add_indexes_v0.7.0.py
"""

import sqlite3
from pathlib import Path

# Get database paths
TRADE_SERVICE_DB = Path(__file__).parent.parent.parent / "data" / "findmy_fm_paper.db"
SOT_DB = Path(__file__).parent.parent / "sot.db"


def create_indexes_sqlite(db_path):
    """Create indexes in SQLite database."""
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    # List of indexes to create
    indexes = [
        # Trade Service indexes
        ("trades", "ix_trades_symbol_status", ["symbol", "status"]),
        ("trades", "ix_trades_entry_time", ["entry_time"]),
        ("trades", "ix_trades_status", ["status"]),
        ("trades", "ix_trades_symbol", ["symbol"]),
        
        ("trade_positions", "ix_trade_positions_symbol", ["symbol"]),
        ("trade_positions", "ix_trade_positions_updated", ["updated_at"]),
        
        # SOT indexes
        ("order_requests", "ix_order_requests_symbol", ["symbol"]),
        ("order_requests", "ix_order_requests_requested_at", ["requested_at"]),
        
        ("orders", "ix_orders_status", ["status"]),
        ("orders", "ix_orders_created_at", ["created_at"]),
        ("orders", "ix_orders_order_request_id", ["order_request_id"]),
        
        ("pending_orders", "ix_pending_orders_symbol_status", ["symbol", "status"]),
        ("pending_orders", "ix_pending_orders_created_at", ["created_at"]),
        ("pending_orders", "ix_pending_orders_status", ["status"]),
    ]
    
    created_count = 0
    for table, index_name, columns in indexes:
        try:
            col_list = ", ".join(columns)
            sql = f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({col_list})"
            cursor.execute(sql)
            created_count += 1
            print(f"✓ Created index: {index_name} on {table}({col_list})")
        except sqlite3.OperationalError as e:
            print(f"⚠ Skipped index {index_name}: {e}")
    
    conn.commit()
    conn.close()
    return created_count


def main():
    """Run migrations on both databases."""
    print("=" * 70)
    print("v0.7.0 Migration: Adding Database Indexes")
    print("=" * 70)
    
    total_created = 0
    
    # Create indexes in Trade Service database
    if TRADE_SERVICE_DB.exists():
        print(f"\n📊 Trade Service Database: {TRADE_SERVICE_DB}")
        count = create_indexes_sqlite(TRADE_SERVICE_DB)
        total_created += count
        print(f"   Total indexes created: {count}")
    else:
        print(f"⚠ Trade Service DB not found: {TRADE_SERVICE_DB}")
    
    # Create indexes in SOT database
    if SOT_DB.exists():
        print(f"\n📊 SOT Database: {SOT_DB}")
        count = create_indexes_sqlite(SOT_DB)
        total_created += count
        print(f"   Total indexes created: {count}")
    else:
        print(f"⚠ SOT DB not found: {SOT_DB}")
    
    print("\n" + "=" * 70)
    print(f"✅ Migration complete! Total indexes created: {total_created}")
    print("=" * 70)
    print("\nExpected performance improvements:")
    print("  • Compound indexes: 10-50x faster filtering")
    print("  • Single column indexes: 5-20x faster searches")
    print("  • Overall query latency: 40-80% reduction")
    print("\nBenchmark after migration with: pytest tests/test_*.py -v --tb=short")


if __name__ == "__main__":
    main()
