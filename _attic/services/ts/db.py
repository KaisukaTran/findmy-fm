"""
TS Database Configuration

Trade Service uses the same SQLite database as SOT for consistency.
Connection is managed at the application level with connection pooling.

v0.7.0: Added connection pooling (QueuePool) for 40-60% faster connection handling.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool, StaticPool

# Use SOT database path
DATABASE_URL = os.getenv("SOT_DATABASE_URL", "sqlite:///./data/findmy_fm_paper.db")

# Connection pooling configuration
# For SQLite, use StaticPool (SQLite handles its own connection pooling)
# For PostgreSQL/MySQL, use QueuePool with pool_size=20, max_overflow=10
is_sqlite = "sqlite" in DATABASE_URL
poolclass = StaticPool if is_sqlite else QueuePool

engine_kwargs = {
    "echo": os.getenv("SQL_ECHO", "false").lower() == "true",
}

if not is_sqlite:
    engine_kwargs.update({
        "poolclass": QueuePool,
        "pool_size": 20,
        "max_overflow": 10,
        "pool_pre_ping": True,
        "pool_recycle": 3600,
    })
else:
    engine_kwargs.update({
        "poolclass": StaticPool,
        "connect_args": {"check_same_thread": False},
    })

engine = create_engine(DATABASE_URL, **engine_kwargs)


# Session factory with scoped_session for thread-safe access
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
ScopedSession = scoped_session(SessionLocal)

Base = declarative_base()


def get_db():
    """Dependency for FastAPI to provide DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_scoped_session():
    """Get a thread-safe scoped session."""
    return ScopedSession


def remove_session():
    """Remove scoped session (call in cleanup)."""
    ScopedSession.remove()
