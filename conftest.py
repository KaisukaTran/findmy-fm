"""Pytest configuration and fixtures."""

import pytest
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Import models to ensure they're registered with SQLAlchemy
from services.sot.pending_orders import PendingOrder
from services.ts.models import Trade, TradePnL, TradePosition, TradePerformance
from services.sot.db import engine as sot_engine
from services.ts.db import engine as ts_engine
from services.sot.db import Base as SotBase
from services.ts.models import Base as TsBase


# Configure pytest timeout for slow tests
def pytest_configure(config):
    """Configure pytest plugins and settings."""
    # Initialize database schemas
    SotBase.metadata.create_all(bind=sot_engine)
    TsBase.metadata.create_all(bind=ts_engine)
    
    # Add timeout marker documentation
    config.addinivalue_line(
        "markers",
        "timeout(seconds): set timeout for test (overrides global timeout)"
    )


# Fixtures for common test setup
@pytest.fixture(autouse=True)
def cleanup_databases():
    """Clean up test databases before and after each test."""
    yield
    # Cleanup happens here (after test runs)


@pytest.fixture
def mock_market_data():
    """Mock market data for testing."""
    return {
        "BTC": 65000.0,
        "ETH": 3500.0,
        "SOL": 180.0,
    }


@pytest.fixture
def mock_exchange_info():
    """Mock exchange info for testing."""
    return {
        "BTC": {
            "symbol": "BTC",
            "minQty": 0.00001,
            "maxQty": 10000.0,
            "stepSize": 0.00001,
            "minNotional": 10.0,
        },
        "ETH": {
            "symbol": "ETH",
            "minQty": 0.001,
            "maxQty": 100000.0,
            "stepSize": 0.001,
            "minNotional": 10.0,
        },
    }
