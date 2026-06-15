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
from app.orchestrator import models as _opus_models  # noqa: E402,F401  (register OPUS tables)


@pytest.fixture(autouse=True)
def _schema():
    """Create a fresh schema for every test, drop it afterwards."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


# Runtime-mutable flags on the global `settings` singleton. Endpoints and the
# runtime/circuit/notify layers flip these in-process, so a test that toggles one
# (e.g. via runtime.full_auto_on) would otherwise leak state into later tests.
_MUTABLE_SETTINGS = (
    "auto_trade", "autoapprove_enabled", "autoapprove_max_notional",
    "full_auto", "scheduler_enabled", "scan_interval_min",
    "guardian_enabled", "telegram_enabled",
    "opus_mode", "opus_shadow", "opus_allocation_usd", "opus_daily_cost_cap_usd",
    "grok_enabled", "grok_scanner_enabled",
    "scan_distance_pct", "scan_tp_pct", "scan_max_waves", "scan_fund",
    "sl_pct", "trailing_pct", "deadline_days",
    "scan_max_symbols", "min_quote_volume", "kss_first_wave_usd",
    "max_concurrent_sessions", "max_sessions_per_symbol",
    "live_trading", "live_max_order_notional",
    "maker_orders", "order_fill_timeout_sec", "live_use_testnet",
    "telegram_notify_trades", "telegram_notify_risk", "telegram_digest_hours",
)


# Automation on/off flags that must start each test at their MODEL DEFAULT (all
# off), so a developer's local .env (e.g. GUARDIAN_ENABLED=true, GROK_ENABLED=true)
# does not leak into tests that assert the default-off behaviour. Numeric strategy
# params are NOT reset here — tests that depend on them set them explicitly, and
# resetting them could change unrelated assertions.
_FLAG_SETTINGS = (
    "auto_trade", "autoapprove_enabled", "full_auto", "scheduler_enabled",
    "guardian_enabled", "telegram_enabled", "opus_mode", "opus_shadow",
    "grok_enabled", "grok_scanner_enabled", "live_trading",
)


@pytest.fixture(autouse=True)
def _settings_guard():
    """Snapshot/restore mutable automation settings around every test, and reset the
    on/off flags to their model defaults at setup so tests are hermetic vs the .env."""
    from app.config import settings

    saved = {name: getattr(settings, name) for name in _MUTABLE_SETTINGS}
    fields = type(settings).model_fields
    for name in _FLAG_SETTINGS:
        if name in fields:
            setattr(settings, name, fields[name].default)
    yield
    for name, value in saved.items():
        setattr(settings, name, value)


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
