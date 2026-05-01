"""Tool implementations that the AI agent can call via Claude tool use."""

import logging
from typing import Any
from datetime import datetime

logger = logging.getLogger(__name__)

# Claude tool schema definitions
TOOL_DEFINITIONS = [
    {
        "name": "get_market_price",
        "description": "Get current market price(s) for one or more symbols",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of trading symbols e.g. ['BTC/USDT', 'ETH/USDT']"
                }
            },
            "required": ["symbols"]
        }
    },
    {
        "name": "get_ohlcv",
        "description": "Get historical OHLCV candlestick data for a symbol",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Trading symbol e.g. 'BTC/USDT'"},
                "timeframe": {"type": "string", "description": "Candle timeframe: 1m, 5m, 15m, 1h, 4h, 1d"},
                "limit": {"type": "integer", "description": "Number of candles (max 100)", "default": 50}
            },
            "required": ["symbol", "timeframe"]
        }
    },
    {
        "name": "get_positions",
        "description": "Get current open positions and account summary",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_daily_pnl",
        "description": "Get today's realized P&L and progress toward daily target",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_exchange_limits",
        "description": "Get exchange order limits for a symbol (min qty, step size, min notional)",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"}
            },
            "required": ["symbol"]
        }
    },
]


def execute_tool(tool_name: str, tool_input: dict) -> Any:
    """Dispatch a tool call to the appropriate implementation."""
    handlers = {
        "get_market_price": _get_market_price,
        "get_ohlcv": _get_ohlcv,
        "get_positions": _get_positions,
        "get_daily_pnl": _get_daily_pnl,
        "get_exchange_limits": _get_exchange_limits,
    }
    handler = handlers.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return handler(**tool_input)
    except Exception as e:
        logger.error(f"Tool {tool_name} error: {e}")
        return {"error": str(e)}


def _get_market_price(symbols: list[str]) -> dict:
    from src.findmy.services.market_data import get_current_prices
    try:
        prices = get_current_prices(symbols)
        return {"prices": prices, "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        return {"error": str(e)}


def _get_ohlcv(symbol: str, timeframe: str = "1h", limit: int = 50) -> dict:
    from src.findmy.services.market_data import get_historical_ohlcv
    try:
        candles = get_historical_ohlcv(symbol, timeframe, min(limit, 100))
        # Return summary stats + last N candles to keep token usage low
        if candles:
            closes = [c[4] for c in candles]
            highs = [c[2] for c in candles]
            lows = [c[3] for c in candles]
            recent = candles[-10:]  # last 10 candles full data
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "candle_count": len(candles),
                "price_range": {"high": max(highs), "low": min(lows)},
                "latest_close": closes[-1],
                "price_change_pct": round((closes[-1] - closes[0]) / closes[0] * 100, 3),
                "recent_candles": [
                    {"time": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
                    for c in recent
                ]
            }
        return {"error": "No candle data returned"}
    except Exception as e:
        return {"error": str(e)}


def _get_positions() -> dict:
    try:
        from services.ts.db import SessionLocal
        from services.ts.models import Trade
        db = SessionLocal()
        try:
            open_trades = db.query(Trade).filter(Trade.status == "OPEN").all()
            positions = [
                {
                    "symbol": t.symbol,
                    "side": t.side,
                    "qty": t.entry_qty,
                    "entry_price": t.entry_price,
                    "unrealized_pnl": None,
                }
                for t in open_trades
            ]
            return {"positions": positions, "count": len(positions)}
        finally:
            db.close()
    except Exception as e:
        return {"positions": [], "error": str(e)}


def _get_daily_pnl() -> dict:
    try:
        from services.risk.risk_management import get_daily_loss, get_account_equity
        from src.findmy.config import settings
        equity = get_account_equity()
        daily_loss = get_daily_loss()
        target_usdt = equity * settings.ai_daily_target_pct / 100
        return {
            "equity": equity,
            "daily_loss_usdt": daily_loss,
            "daily_target_usdt": target_usdt,
            "target_pct": settings.ai_daily_target_pct,
            "target_reached": daily_loss < 0 and abs(daily_loss) >= target_usdt,
        }
    except Exception as e:
        return {"error": str(e)}


def _get_exchange_limits(symbol: str) -> dict:
    try:
        from src.findmy.services.market_data import get_exchange_info
        info = get_exchange_info(symbol)
        return info
    except Exception as e:
        return {"error": str(e)}
