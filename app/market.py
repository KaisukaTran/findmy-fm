"""
Market data for FINDMY-FM (lean rebuild).

Fetches public spot prices and lot-size info from Binance via ccxt, with a small
in-process TTL cache to avoid rate limits. No API key required (public data only).
All network failures degrade gracefully to cached/last-known or safe-default values.
"""

from __future__ import annotations

import logging
import time

import ccxt

from app.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_INFO = {
    "symbol": "",
    "minQty": 0.00001,
    "maxQty": 10000.0,
    "stepSize": 0.00001,
    "minNotional": 10.0,
}

_price_cache: dict[str, float] = {}
_price_cache_ts: float = 0.0
_exchange_info_cache: dict[str, dict] = {}


def _exchange() -> ccxt.binance:
    return ccxt.binance()


def get_current_prices(symbols: list[str]) -> dict[str, float]:
    """Return {symbol: usd_price} for the given base symbols, using a TTL cache."""
    global _price_cache_ts
    if not symbols:
        return {}

    fresh = (time.time() - _price_cache_ts) < settings.price_cache_ttl
    cached = {s: _price_cache[s] for s in symbols if fresh and s in _price_cache}
    missing = [s for s in symbols if s not in cached]
    if not missing:
        return cached

    fetched: dict[str, float] = {}
    try:
        ex = _exchange()
        for symbol in missing:
            try:
                ticker = ex.fetch_ticker(f"{symbol}/USDT")
                fetched[symbol] = float(ticker["last"])
            except Exception:  # one bad symbol shouldn't fail the batch
                continue
    except Exception as exc:  # whole exchange unavailable
        logger.warning("Binance price fetch failed: %s", exc)
        return cached

    if fetched:
        _price_cache.update(fetched)
        _price_cache_ts = time.time()
    return {**cached, **fetched}


def get_exchange_info(symbol: str) -> dict:
    """Return lot-size/precision info for a symbol; cached; safe defaults on failure."""
    if symbol in _exchange_info_cache:
        return _exchange_info_cache[symbol]
    try:
        market = _exchange().market(f"{symbol}/USDT")
        limits = market.get("limits", {})
        amount = limits.get("amount", {})
        cost = limits.get("cost", {})
        info = {
            "symbol": symbol,
            "minQty": amount.get("min") or 0.00001,
            "maxQty": amount.get("max") or 10000.0,
            "stepSize": market.get("precision", {}).get("amount") or 0.00001,
            "minNotional": cost.get("min") or 10.0,
        }
        _exchange_info_cache[symbol] = info
        return info
    except Exception as exc:
        logger.warning("Binance exchange info failed for %s: %s", symbol, exc)
        return {**_DEFAULT_INFO, "symbol": symbol}


def get_unrealized_pnl(
    symbol: str, quantity: float, avg_price: float, current_price: float | None = None
) -> tuple[float, float]:
    """Return (unrealized_pnl, market_value) for a position."""
    if current_price is None:
        current_price = get_current_prices([symbol]).get(symbol)
        if not current_price:
            return 0.0, 0.0
    market_value = quantity * current_price
    return market_value - (quantity * avg_price), market_value


def clear_cache() -> None:
    """Clear all in-process caches (used in tests)."""
    global _price_cache_ts
    _price_cache.clear()
    _exchange_info_cache.clear()
    _price_cache_ts = 0.0
