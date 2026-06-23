"""
Database layer for FINDMY-FM (single SQLite database, all tables).

Exposes:
- engine / SessionLocal — SQLAlchemy 2.0 session factory
- Base — declarative base for models
- get_db() — FastAPI dependency yielding a session (always closed)
- init_db() — create all tables (called from app lifespan)
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# Columns added after a table first shipped. create_all() never ALTERs an
# existing table, so we add any missing columns by hand (idempotent). Each entry
# is (table, column, SQL column definition).
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("kss_sessions", "sl_pct", "FLOAT NOT NULL DEFAULT 0.0"),
    ("kss_sessions", "trailing_pct", "FLOAT NOT NULL DEFAULT 0.0"),
    ("kss_sessions", "peak_price", "FLOAT NOT NULL DEFAULT 0.0"),
    ("kss_sessions", "trail_active", "BOOLEAN NOT NULL DEFAULT 0"),
    ("kss_sessions", "trail_sl_price", "FLOAT NOT NULL DEFAULT 0.0"),
    ("kss_sessions", "trail_dist_pct", "FLOAT NOT NULL DEFAULT 0.0"),
    ("pending_orders", "auto_veto", "BOOLEAN NOT NULL DEFAULT 0"),
    ("pending_orders", "auto_veto_reason", "TEXT"),
    ("pending_orders", "auto_veto_at", "DATETIME"),
    ("pending_orders", "exchange_order_id", "VARCHAR(64)"),
    ("pending_orders", "exchange_status", "VARCHAR(16)"),
    ("kss_waves", "exchange_order_id", "VARCHAR(64)"),
    ("kss_waves", "exchange_status", "VARCHAR(16)"),
    ("candidates", "win_rate_lb", "FLOAT NOT NULL DEFAULT 0.0"),
    ("candidates", "expectancy", "FLOAT NOT NULL DEFAULT 0.0"),
    ("candidates", "trials", "INTEGER NOT NULL DEFAULT 0"),
    ("candidates", "avg_mae", "FLOAT NOT NULL DEFAULT 0.0"),
    ("candidates", "worst_mae", "FLOAT NOT NULL DEFAULT 0.0"),
    ("kss_sessions", "strategy_mode", "TEXT DEFAULT 'dca_down'"),
]


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def _ensure_sqlite_dir(database_url: str) -> None:
    """Make sure the directory for a SQLite file exists."""
    if database_url.startswith("sqlite") and ":memory:" not in database_url:
        # sqlite:///./data/findmy.db -> ./data/findmy.db
        path = database_url.split("///", 1)[-1]
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

# check_same_thread=False is required for SQLite under the async server's thread pool.
# timeout makes a writer wait for the lock (busy_timeout) instead of failing immediately —
# the OPUS loop and the rule-based scheduler both write, so brief contention is expected.
_connect_args = (
    {"check_same_thread": False, "timeout": 30.0}
    if settings.database_url.startswith("sqlite")
    else {}
)

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


if settings.database_url.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        """WAL so readers never block the writer (the dashboard polls constantly while the
        scheduler/OPUS/withdrawals write) + a busy_timeout so a writer waits for the lock
        instead of raising 'database is locked'. Set per-connection on connect."""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a DB session and guarantee it is closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_columns() -> None:
    """Add any missing columns to existing tables (idempotent lightweight migration)."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, column, ddl in _ADDED_COLUMNS:
        if table not in existing_tables:
            continue  # create_all already built it with the column
        cols = {c["name"] for c in inspector.get_columns(table)}
        if column in cols:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db() -> None:
    """Create all tables, then apply additive column migrations. Imports models so
    they register on Base.metadata."""
    from app import models  # noqa: F401  (registers models on Base)
    from app.orchestrator import models as _opus_models  # noqa: F401  (OPUS tables)

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
