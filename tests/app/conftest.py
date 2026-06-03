"""
Test fixtures for the lean app/ rebuild.

Isolated from the legacy root conftest: this directory is its own pytest rootdir
(see tests/app/pytest.ini). We point the app at a throwaway SQLite file BEFORE
importing any app module, then create/drop the schema around each test.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure the project root (which contains the `app` package) is importable.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

# Point the app at a temporary database before app.config is imported.
_TMP_DIR = tempfile.mkdtemp(prefix="findmy_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR}/test.db"
os.environ["REQUIRE_AUTH"] = "false"

from app import models  # noqa: E402,F401  (register models on Base)
from app.db import Base, SessionLocal, engine  # noqa: E402


@pytest.fixture(autouse=True)
def _schema():
    """Create a fresh schema for every test, drop it afterwards."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    """A DB session bound to the temporary test database."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def mock_prices():
    return {"BTC": 65000.0, "ETH": 3500.0, "SOL": 180.0}


@pytest.fixture
def mock_exchange_info():
    return {"symbol": "BTC", "minQty": 0.00001, "maxQty": 10000.0, "stepSize": 0.00001, "minNotional": 10.0}
