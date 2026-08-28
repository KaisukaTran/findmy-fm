"""
Real-time Binance price feed (public WebSocket, no API key) — LIVE INSTANCE ONLY.

Streams ``!miniTicker@arr`` and pushes parsed last-prices into ``app.market``'s
existing TTL cache via ``market.note_ws_prices`` so trailing-stop / crash-detect
checks react against a sub-second price instead of waiting for the next REST
poll. Gated entirely by ``settings.live_trading`` at the call site (``app.main``
lifespan) — this module never starts itself and the paper instance never
imports/uses it.

Public surface
---------------
parse_mini_ticker(payload, quote) -- pure parser, no network/imports of market.
BinancePriceFeed                   -- reconnecting WS client.
start() / stop()                   -- module-level singleton lifecycle (called
                                       from the asyncio loop in app.main).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

import websockets

from app.config import settings
from app.data.providers import _QUOTES

logger = logging.getLogger(__name__)

_DEFAULT_URL = "wss://stream.binance.com:9443/ws/!miniTicker@arr"


def parse_mini_ticker(payload: list | dict, quote: str) -> dict[str, float]:
    """Parse a Binance ``!miniTicker@arr`` push into ``{base_symbol: price}``.

    Each element looks like ``{"s": "BTCUSDT", "c": "1234.5", ...}``. Only
    symbols ending with ``quote`` are kept; the base is the symbol with that
    suffix stripped. Elements with a different quote or a missing/zero close
    price are ignored. Accepts a single dict defensively (treated as a
    one-element array). Pure — no network calls, no import of app.market.
    """
    items = payload if isinstance(payload, list) else [payload]
    out: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = item.get("s")
        close = item.get("c")
        if not symbol or not isinstance(symbol, str) or not symbol.endswith(quote):
            continue
        if close is None:
            continue
        try:
            price = float(close)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        base = symbol[: -len(quote)]
        if not base:
            continue
        out[base] = price
    return out


class BinancePriceFeed:
    """Reconnecting client for Binance's public ``!miniTicker@arr`` stream."""

    def __init__(
        self,
        quote: str = "USDT",
        url: str = _DEFAULT_URL,
        on_prices: Callable[[dict[str, float]], None] | None = None,
    ) -> None:
        self.quote = quote
        self.url = url
        self.on_prices = on_prices
        self._stop = False
        self._connected = False
        self._last_msg_ts = 0.0

    def is_fresh(self, max_age: float) -> bool:
        """True if connected and a message has arrived within ``max_age`` seconds."""
        return self._connected and (time.time() - self._last_msg_ts) < max_age

    async def run(self) -> None:
        """Connect and consume the stream forever, reconnecting with backoff.

        Never raises — all exceptions/disconnects are caught, logged, and
        retried with exponential backoff (1s -> 2s -> ... capped at 30s),
        reset to 1s after each successful message. Exits cleanly on
        ``asyncio.CancelledError`` or when ``self._stop`` is set.
        """
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(self.url) as ws:
                    logger.info("ws_feed: connected to %s", self.url)
                    async for raw in ws:
                        if self._stop:
                            break
                        backoff = 1.0
                        self._connected = True
                        self._last_msg_ts = time.time()
                        try:
                            prices = parse_mini_ticker(json.loads(raw), self.quote)
                        except Exception as exc:  # malformed payload — skip, stay connected
                            logger.warning("ws_feed: parse error: %s", exc)
                            continue
                        if prices and self.on_prices is not None:
                            self.on_prices(prices)
            except asyncio.CancelledError:
                self._connected = False
                raise
            except Exception as exc:
                logger.warning("ws_feed: disconnected (%s); reconnecting in %.0fs", exc, backoff)
            else:
                logger.info("ws_feed: stream closed; reconnecting in %.0fs", backoff)
            self._connected = False
            if self._stop:
                break
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, 30.0)

    def stop(self) -> None:
        """Signal ``run()`` to exit at the next opportunity."""
        self._stop = True


# ---------------------------------------------------------------------------
# Module-level singleton lifecycle (mirrors app/notify.py)
# ---------------------------------------------------------------------------

_task: asyncio.Task | None = None
_feed: BinancePriceFeed | None = None


def start() -> bool:
    """Start the live price feed if not already running. Idempotent.

    Caller (app.main lifespan) is responsible for gating this on
    ``settings.live_trading`` / ``settings.live_ws_prices`` / exchange — this
    function itself starts unconditionally when called.
    """
    global _task, _feed
    if _task and not _task.done():
        return False
    from app import market

    quote = _QUOTES.get(settings.live_exchange, "USDT")
    _feed = BinancePriceFeed(quote=quote, on_prices=market.note_ws_prices)
    market.register_ws_feed(_feed)
    _task = asyncio.create_task(_feed.run())
    return True


def stop() -> bool:
    """Cancel the feed task and unregister it from app.market. Safe if never started."""
    global _task, _feed
    from app import market

    market.unregister_ws_feed()
    if _feed is not None:
        _feed.stop()
    if _task and not _task.done():
        _task.cancel()
        _task = None
        _feed = None
        return True
    _task = None
    _feed = None
    return False
