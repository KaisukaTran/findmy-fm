"""
TS Database Configuration

Trade Service uses the same SQLite database as SOT for consistency.
Connection is managed at the application level.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Use SOT database path
DATABASE_URL = os.getenv("SOT_DATABASE_URL", "sqlite:///./data/findmy_fm_paper.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    echo=os.getenv("SQL_ECHO", "false").lower() == "true",
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency for FastAPI to provide DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
