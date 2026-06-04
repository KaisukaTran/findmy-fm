"""
Market data for FINDMY-FM (lean rebuild).

Thin TTL-caching layer over the pluggable ccxt provider (`app.data.providers`),
so prices and lot-size info come from the configured `live_exchange` — not a
hardcoded venue. No API key required (public data only). All network failures
degrade gracefully to cached/last-known or safe-default values.
"""

from __future__ import annotations

import logging
import time

from app.config import settings
from app.data.providers import live_provider

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

    try:
        fetched = live_provider().get_prices(missing)
    except Exception as exc:  # whole exchange unavailable
        logger.warning("%s price fetch failed: %s", settings.live_exchange, exc)
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
        info = live_provider().get_exchange_info(symbol)
    except Exception as exc:
        logger.warning("%s exchange info failed for %s: %s", settings.live_exchange, symbol, exc)
        return {**_DEFAULT_INFO, "symbol": symbol}
    _exchange_info_cache[symbol] = info
    return info


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
