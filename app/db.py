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

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


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
_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a DB session and guarantee it is closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables. Imports models so they register on Base.metadata."""
    from app import models  # noqa: F401  (registers models on Base)

    Base.metadata.create_all(bind=engine)
