"""
In-process OHLCV candle cache.

Design
------
- Keyed by ``(exchange_id, symbol, timeframe)`` → ``{fetched_at, candles}``.
- TTL = one bar period (e.g. 86400 s for "1d").  Minimum TTL is 15 minutes so
  short-period timeframes (1m, 5m …) are not fetched every scan cycle.
- On expiry we fetch only the *tail* (since=last cached timestamp) and merge —
  not the full history — so re-warm is cheap.
- Read-through / crash-safe: any network error returns [] and is *not* cached
  so the next call retries cleanly.  Behaviour on failure is identical to the
  pre-cache code (empty list → symbol skipped via existing skipped_thin_data
  audit).

Thread-safety choice (one-client-per-worker)
--------------------------------------------
ccxt sync exchange instances share internal HTTP session state and are **not**
thread-safe across concurrent callers (see ccxt docs §"Synchronous vs Asynchronous").
We therefore pass a *factory callable* to the parallel worker so each thread
constructs its own fresh CcxtProvider instance.  No locks needed; no shared
mutable exchange object.  The alternative (a semaphore around a single client)
serialises all fetches and gives no parallelism benefit.  ccxt async would be
the cleaner long-term approach but drags the full scan into async — deferred to
a later phase (S2-follow-up).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from app.data.providers import Candle, CcxtProvider

logger = logging.getLogger(__name__)

# Minimum TTL regardless of the timeframe (15 minutes in seconds).
_MIN_TTL_S: float = 15 * 60

# Timeframe string → seconds per bar.
_TF_SECONDS: dict[str, float] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
}


def _ttl_seconds(timeframe: str) -> float:
    """Return the effective TTL for ``timeframe``.

    TTL = one bar period, floored to ``_MIN_TTL_S`` so very short timeframes
    are not fetched on every 15-minute scan cycle.
    """
    return max(_MIN_TTL_S, _TF_SECONDS.get(timeframe, 86400))


class _CacheEntry:
    __slots__ = ("fetched_at", "candles")

    def __init__(self, candles: list[Candle]) -> None:
        self.fetched_at: float = time.monotonic()
        self.candles: list[Candle] = candles

    def is_fresh(self, timeframe: str) -> bool:
        """True when the entry is still within its TTL."""
        return (time.monotonic() - self.fetched_at) < _ttl_seconds(timeframe)

    def last_ts_ms(self) -> int | None:
        """Millisecond timestamp of the newest cached candle (or None)."""
        return self.candles[-1]["ts"] if self.candles else None


# Module-level cache shared by all callers within the process.
_cache: dict[tuple[str, str, str], _CacheEntry] = {}


def clear() -> None:
    """Empty the cache (used in tests)."""
    _cache.clear()


def get_candles(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    limit: int,
    provider_factory: Callable[[str], CcxtProvider],
) -> tuple[list[Candle], bool]:
    """Return ``(candles, was_cache_hit)`` for the requested series.

    Parameters
    ----------
    exchange_id:
        ccxt exchange id string (e.g. ``"kraken"``).
    symbol:
        Base asset symbol (e.g. ``"BTC"``).
    timeframe:
        ccxt timeframe string (e.g. ``"1d"``).
    limit:
        Number of bars to request from the exchange when a full fetch is needed.
    provider_factory:
        Callable ``exchange_id -> CcxtProvider``.  Called inside the worker so
        each parallel thread gets its own ccxt client (see thread-safety note).
    """
    key = (exchange_id, symbol, timeframe)
    entry = _cache.get(key)

    if entry is not None and entry.is_fresh(timeframe):
        return entry.candles, True  # cache hit — no network call

    # Cache miss or stale.  Determine whether to do a full fetch or a tail fetch.
    since_ms: int | None = None
    if entry is not None and entry.candles:
        # Tail fetch: request bars since the last cached timestamp so we only
        # pull new bars and merge rather than re-downloading the full history.
        since_ms = entry.last_ts_ms()

    try:
        provider = provider_factory(exchange_id)
        if since_ms is not None:
            raw = provider._ex.fetch_ohlcv(
                provider.pair(symbol), timeframe=timeframe, since=since_ms
            )
            new_candles: list[Candle] = [
                Candle(ts=int(c[0]), open=float(c[1]), high=float(c[2]),
                       low=float(c[3]), close=float(c[4]), volume=float(c[5]))
                for c in raw
            ]
            # Merge: keep all existing candles up to (exclusive) the tail ts,
            # then append the fresh tail (exchange may return the boundary bar).
            merged = [c for c in entry.candles if c["ts"] < since_ms] + new_candles  # type: ignore[index]
            candles = merged
        else:
            # Full fetch (cold start or no prior entry).
            candles = provider.get_ohlcv(symbol, timeframe, limit)
    except Exception as exc:
        logger.warning("candle_cache fetch failed (%s %s %s): %s",
                       exchange_id, symbol, timeframe, exc)
        # Degrade gracefully: return whatever was cached (possibly stale or empty).
        return (entry.candles if entry is not None else []), False

    if candles:
        _cache[key] = _CacheEntry(candles)

    return candles, False  # cache miss (we just fetched)
