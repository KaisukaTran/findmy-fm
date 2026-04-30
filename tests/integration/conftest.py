"""Integration test fixtures.

Uses a real SQLite test DB (separate from prod) so tests are isolated
and do not require mocking of database calls.
"""

import os
import sys
import pytest
import tempfile
from pathlib import Path

# Ensure project root is on path
_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

# Set required env vars BEFORE any app imports
os.environ.setdefault("APP_SECRET_KEY", "integration-test-secret-key-32chars!")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-fake-key-for-integration-tests")

# Point all DB connections at a temp file for this session
_tmp_db = tempfile.mktemp(suffix=".db", prefix="findmy_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db}"
os.environ["SOT_DATABASE_URL"] = f"sqlite:///{_tmp_db}"

import sqlite3
from fastapi.testclient import TestClient
from services.auth.service import create_access_token


def _setup_schema(db_path: str) -> None:
    """Apply all Alembic migrations AND ORM create_all to the test DB."""
    from alembic.config import Config
    from alembic import command
    from sqlalchemy import create_engine

    url = f"sqlite:///{db_path}"
    cfg = Config(str(_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    # Also run ORM create_all so that any tables only defined in models
    # (not yet in Alembic) are present for tests
    engine = create_engine(url, connect_args={"check_same_thread": False})
    from services.sot.pending_orders import Base as SotBase
    from services.ts.models import Base as TsBase
    SotBase.metadata.create_all(engine)
    TsBase.metadata.create_all(engine)
    engine.dispose()

    # Patch module-level engines so the whole app uses the test DB
    import services.sot.db as sot_db_mod
    import services.ts.db as ts_db_mod
    _patched = create_engine(url, connect_args={"check_same_thread": False})
    from sqlalchemy.orm import sessionmaker, scoped_session
    sot_db_mod.engine = _patched
    sot_db_mod.SessionLocal = scoped_session(sessionmaker(bind=_patched))
    ts_db_mod.engine = _patched
    ts_db_mod.SessionLocal = scoped_session(sessionmaker(bind=_patched))


@pytest.fixture(scope="session", autouse=True)
def test_db():
    """Create and migrate a fresh test DB for the entire session."""
    _setup_schema(_tmp_db)
    yield _tmp_db
    try:
        Path(_tmp_db).unlink(missing_ok=True)
    except Exception:
        pass


@pytest.fixture(scope="session")
def client(test_db):
    """FastAPI TestClient wired to the test DB."""
    from findmy.api.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def admin_token() -> str:
    """JWT token with admin role."""
    return create_access_token({"sub": "test_admin", "role": "admin", "scopes": ["read", "write"]})


@pytest.fixture
def trader_token() -> str:
    """JWT token with trader (non-admin) role."""
    return create_access_token({"sub": "test_trader", "role": "trader", "scopes": ["read", "write"]})


@pytest.fixture
def admin_headers(admin_token) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def trader_headers(trader_token) -> dict:
    return {"Authorization": f"Bearer {trader_token}"}


@pytest.fixture(autouse=True)
def reset_halt(test_db):
    """Ensure emergency halt is cleared before each test."""
    from services.sot.system_state import set_halt
    set_halt(False)
    yield
    set_halt(False)
