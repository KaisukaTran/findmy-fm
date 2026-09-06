"""
SOT Database Configuration

Source of Truth database with connection pooling for performance.

v0.7.0: Added connection pooling and scoped sessions.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, scoped_session
from sqlalchemy.pool import QueuePool, StaticPool

DATABASE_URL = "sqlite:///db/sot.db"

# Connection pooling configuration
# For SQLite, use StaticPool (SQLite handles its own connection pooling)
# For PostgreSQL/MySQL, use QueuePool with pool_size=20, max_overflow=10
is_sqlite = "sqlite" in DATABASE_URL
poolclass = StaticPool if is_sqlite else QueuePool

engine_kwargs = {
    "echo": False,
    "future": True,
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
    })

engine = create_engine(DATABASE_URL, **engine_kwargs)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True
)

# Scoped session for thread-safe access
ScopedSession = scoped_session(SessionLocal)

Base = declarative_base()


def get_scoped_session():
    """Get a thread-safe scoped session."""
    return ScopedSession


def remove_session():
    """Remove scoped session."""
    ScopedSession.remove()
