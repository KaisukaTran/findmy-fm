"""
TS (Trade Service) Package

Trade Service is responsible for:
- Trade aggregation (entry → exit)
- Trade lifecycle management
- P&L calculations and analytics
- Position tracking and inventory
- Trade performance metrics

TS integrates with SOT (Source of Truth) to read order execution data.
"""

from services.ts.service import TSService
from services.ts import repository
from services.ts.models import Trade, TradePnL, TradePosition, TradePerformance

__all__ = [
    "TSService",
    "repository",
    "Trade",
    "TradePnL",
    "TradePosition",
    "TradePerformance",
]
