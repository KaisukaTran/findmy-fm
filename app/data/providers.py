"""
Pluggable market-data providers built on ccxt (public endpoints only — no API key).

A provider wraps a single ccxt exchange behind one interface. Binance is the only
supported venue (live prices AND historical/scan data) — no other venue should be
configured. Each provider still knows its own quote asset (Binance→USDT) so the
abstraction degrades safely if a non-Binance ccxt id is ever passed in.

All network failures degrade gracefully (empty list / safe defaults) so a flaky
exchange never crashes a scan.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Protocol, TypedDict

import ccxt

from app.execution import note_if_rate_error

logger = logging.getLogger(__name__)

# Fix C (2026-09-01): `fetch_tickers()` costs 80 weight regardless of a `symbols` filter — in
# the installed ccxt 4.0.5, binance.fetch_tickers sends NO symbol filter to the venue at all
# (see ccxt's binance.py: `response = getattr(self, method)(query)` with `query` holding only
# `params`, never `symbols` — the `symbols` arg is applied CLIENT-side by `parse_tickers` after
# the full-universe response is already back). Current Binance `GET /api/v3/ticker/24hr`
# weight: 2 for a single symbol, 40 for 21-100 symbols, 80 when the symbol filter is omitted
# (the full-universe response this call always triggers). get_prices/top_symbols/all_symbols
# each called this independently once per scan cycle, so the same 80-weight full fetch was
# paid up to 3x. One TTL cache of the raw map, shared by all three, cuts that to at most once
# per TTL window.
_TICKERS_TTL_SEC = 60.0

# ccxt id -> quote asset used for that exchange's USD-ish spot pairs.
# Binance is the only venue this app configures at runtime; the rest stay only
# because tests exercise the abstraction (offline, no network) against them.
_QUOTES = {
    "binance": "USDT",
    "binanceus": "USDT",
    "kucoin": "USDT",
    "okx": "USDT",
    "coinbase": "USD",
    "coinbasepro": "USD",
    "bitstamp": "USD",
}

# Bases with no crypto directional alpha — dropped from the scan universe so they don't
# waste scan budget or pollute the "top by volume" list (stables/fiat dominate volume).
#   * Stablecoins / pegged USD tokens — a stable-vs-stable peg (e.g. USDC/USDT, FDUSD/USDT).
#   * Fiat / forex currencies — an FX pair, not crypto (e.g. EUR/USDT, GBP/USDT, AUD/USDT).
_STABLES = {
    "USDT", "USDC", "USD1", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD",
    "PYUSD", "GUSD", "USDE", "USTC", "SUSD", "LUSD", "USDS", "FRAX", "USDJ",
    "CUSD", "USDX", "XUSD", "EURT", "EURS", "AEUR", "EURI",
}
_FIAT = {
    "EUR", "GBP", "AUD", "JPY", "CHF", "CAD", "NZD", "CNY", "TRY", "BRL", "RUB",
    "ZAR", "MXN", "ARS", "PLN", "RON", "UAH", "IDR", "NGN", "VND", "KRW", "INR",
    "SGD", "HKD", "THB", "PHP", "MYR", "CZK", "HUF", "BGN", "DKK", "SEK", "NOK",
}


def _is_excluded_base(base: str) -> bool:
    """True for stablecoins and fiat/FX bases — pairs carrying no crypto directional alpha."""
    b = base.upper()
    return b in _STABLES or b in _FIAT


_DEFAULT_INFO = {
    "symbol": "",
    "minQty": 0.00001,
    "maxQty": 10000.0,
    "stepSize": 0.00001,
    "minNotional": 10.0,
}


class Candle(TypedDict):
    """One OHLCV bar."""

    ts: int  # epoch milliseconds
    open: float
    high: float
    low: float
    close: float
    volume: float


# Binance answers at most 1000 klines per request — and silently truncates instead of
# erroring when asked for more, which is why anything longer has to be paged.
_MAX_KLINES = 1000
# Spare pages so a venue that returns slightly short batches (or a gap in history) still
# reaches the requested window, while the loop stays bounded.
_PAGE_SLACK = 3


def _to_candle(c: list) -> Candle:
    return Candle(ts=int(c[0]), open=float(c[1]), high=float(c[2]),
                  low=float(c[3]), close=float(c[4]), volume=float(c[5]))


def _step_size_from_market(precision_mode: object, market: dict) -> float:
    """Derive the true lot-size (quantity) step for a ccxt market.

    ``market['precision']['amount']`` means different things depending on the exchange's
    ``precisionMode`` — for Binance (``DECIMAL_PLACES``) it is a *count of decimals*
    (e.g. ``3`` for BNB → step ``0.001``), not a step itself. Using it directly as a step
    made a $15 wave on any coin above ~$17 collapse toward ``minQty`` (37/361 universe
    symbols) — see docs/capital-scaling-2026-08-23.md §2.2.

    Preference order:
      1. The exchange's own ``LOT_SIZE`` filter (``market['info']['filters']``) — the
         ground-truth quantity increment, verified correct for every Binance symbol.
      2. ``precision.amount`` interpreted per ``precisionMode``:
         - ``DECIMAL_PLACES`` → ``10 ** -precision`` (precision is a decimal count).
         - ``TICK_SIZE``      → the value already IS the step.
         - anything else (``SIGNIFICANT_DIGITS`` or unknown/missing mode) → no reliable
           formula; fall through.
      3. ``limits.amount.min`` (the exchange's minimum tradeable quantity — a safe step
         floor when nothing more precise is available).
      4. The module default (``0.00001``).
    """
    info = market.get("info") or {}
    for f in info.get("filters", []) or []:
        if f.get("filterType") == "LOT_SIZE":
            step = f.get("stepSize")
            if step:
                try:
                    return float(step)
                except (TypeError, ValueError):
                    break  # malformed filter — fall through to precision/limits

    precision = (market.get("precision") or {}).get("amount")
    if precision is not None:
        if precision_mode == ccxt.DECIMAL_PLACES:
            try:
                return 10 ** -float(precision)
            except (TypeError, ValueError, OverflowError):
                pass
        elif precision_mode == ccxt.TICK_SIZE:
            try:
                step = float(precision)
                if step > 0:
                    return step
            except (TypeError, ValueError):
                pass
        # SIGNIFICANT_DIGITS or an unknown/missing mode: precision.amount is not a step
        # and not a decimal count we can convert safely — deliberately do not guess.

    min_qty = (market.get("limits") or {}).get("amount", {}).get("min")
    if min_qty:
        try:
            return float(min_qty)
        except (TypeError, ValueError):
            pass
    return 0.00001


class DataProvider(Protocol):
    """Read-only market data surface used by market.py, the scanner and backtests."""

    def get_prices(self, symbols: list[str], fresh: bool = False) -> dict[str, float]: ...
    def get_ohlcv(self, symbol: str, timeframe: str = "1d", limit: int = 200) -> list[Candle]: ...
    def top_symbols(self, n: int = 10) -> list[str]: ...
    def all_symbols(self, min_quote_volume: float = 0.0) -> list[str]: ...
    def get_exchange_info(self, symbol: str) -> dict: ...


class CcxtProvider:
    """DataProvider backed by a single ccxt exchange (public data only)."""

    def __init__(self, exchange_id: str, quote: str | None = None):
        self.exchange_id = exchange_id
        self.quote = quote or _QUOTES.get(exchange_id, "USDT")
        # enableRateLimit makes ccxt pace requests to the exchange's per-IP limit (≈20 req/s for
        # Binance). It defaults to True in current ccxt, but set it explicitly so an upstream
        # default change can never silently remove our only client-side throttle → IP ban risk.
        self._ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        # Fix C: the shared full-universe fetch_tickers() cache (see _fetch_tickers_map).
        self._tickers_cache: dict[str, dict] | None = None
        self._tickers_cache_ts: float = 0.0

    def pair(self, symbol: str) -> str:
        """Map a base symbol (e.g. 'BTC') to this exchange's pair (e.g. 'BTC/USD')."""
        return f"{symbol}/{self.quote}"

    def _fetch_tickers_map(self, force_refresh: bool = False) -> dict[str, dict]:
        """Raw ``fetch_tickers()`` result (every pair on this exchange), cached for
        ``_TICKERS_TTL_SEC`` and shared by ``get_prices``/``top_symbols``/``all_symbols``
        (Fix C). Called with NO ``symbols`` filter deliberately — see the module note — so
        every caller derives its own view (a symbol lookup, or a volume-sorted/filtered list)
        from the same raw map instead of each paying its own full-universe fetch.

        Raises through unchanged on a failed fetch — the cache is simply left as it was (the
        last good map + its timestamp), so a rate-classified error never poisons it with an
        empty result; callers decide how to degrade (see get_prices/top_symbols/all_symbols).

        An EMPTY (but non-raising) result is a degenerate success, not a real snapshot — the
        venue answered with nothing to show for it. Caching that would serve zero
        prices/zero symbols to every caller for the full TTL, so the empty map is returned
        as-is but the cache (and its timestamp) is left untouched, letting the NEXT call
        retry the network instead of parroting the empty map for up to ``_TICKERS_TTL_SEC``.
        """
        now = time.monotonic()
        if (not force_refresh and self._tickers_cache is not None
                and (now - self._tickers_cache_ts) < _TICKERS_TTL_SEC):
            return self._tickers_cache
        tickers = self._ex.fetch_tickers()
        if not tickers:
            return tickers
        self._tickers_cache = tickers
        self._tickers_cache_ts = now
        return tickers

    def get_prices(self, symbols: list[str], fresh: bool = False) -> dict[str, float]:
        """Batch-fetch prices from the shared ticker cache (Fix C, one call/TTL window when
        the exchange supports it); fall back to per-symbol ``fetch_ticker`` when that fetch
        itself fails for a NON-rate reason (B9 — an exchange without ``fetch_tickers``, etc).

        Fix B2: a 429/418/-1015 on the batched call must not amplify into one request PER
        SYMBOL — such an error is noted (``execution.note_rate_error``, via
        ``note_if_rate_error``) and this returns immediately with whatever was already
        resolved (nothing, on a cold batch). A rate-classified error discovered mid-fallback
        stops that loop too, for the same reason.
        """
        if not symbols:
            return {}
        pairs = [self.pair(s) for s in symbols]
        out: dict[str, float] = {}
        try:
            # ``fresh=True`` pierces the shared TTL cache: the 90s position-guard's forced tick
            # sizes hard-SL decisions, and "exits are never gated" extends to never being fed a
            # cached price when the caller explicitly demanded a live one (WS feed down + SL
            # armed is exactly the moment it matters). Scan paths never pass it.
            tickers = self._fetch_tickers_map(force_refresh=fresh)
        except Exception as exc:
            if note_if_rate_error(exc, self._ex):
                return out
            # Exchange doesn't support batched fetch_tickers (or some other non-rate error) —
            # fall back per-symbol.
            for symbol in symbols:
                try:
                    out[symbol] = float(self._ex.fetch_ticker(self.pair(symbol))["last"])
                except Exception as exc2:
                    if note_if_rate_error(exc2, self._ex):
                        break  # a rate error mid-fallback must stop the loop, not keep hammering
                    continue
            return out
        for symbol, pair in zip(symbols, pairs, strict=False):
            t = tickers.get(pair) or {}
            last = t.get("last")
            if last is not None:
                out[symbol] = float(last)
        return out

    def get_ohlcv(self, symbol: str, timeframe: str = "1d", limit: int = 200) -> list[Candle]:
        """Fetch the last *limit* candles, paging when that is more than one response holds.

        Binance caps klines at 1000 per request and does NOT complain when asked for more —
        it silently answers 1000. On a daily timeframe that never mattered (a year is 365
        bars); on 5m the same year is 105,120, so a single call would have returned 3.5 days
        of data while every label said a year. Above one page we therefore walk forward from
        a computed start with ``since``, oldest page first, and join.

        Failures keep whatever was already fetched — partial history beats none — and paging
        stops on an unknown timeframe (no bar duration = no cursor to walk), on a page that
        does not advance, and at a page budget, so it can never spin.
        """
        pair = self.pair(symbol)
        tf_ms = self._timeframe_ms(timeframe)
        if limit <= _MAX_KLINES or tf_ms <= 0:
            try:
                raw = self._ex.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
            except Exception as exc:
                # Fix B3: note a rate-classified error (429/418/-1015) so the NEXT call
                # refuses instead of hammering; degrade exactly as before either way.
                note_if_rate_error(exc, self._ex)
                logger.warning("%s OHLCV failed for %s: %s", self.exchange_id, symbol, exc)
                return []
            return [_to_candle(c) for c in raw]

        # Start far enough back that `limit` bars fit, then page forward to now.
        cursor = self._ex.milliseconds() - limit * tf_ms
        pages = math.ceil(limit / _MAX_KLINES) + _PAGE_SLACK
        rows: list[list] = []
        for _ in range(pages):
            try:
                batch = self._ex.fetch_ohlcv(
                    pair, timeframe=timeframe, since=cursor, limit=_MAX_KLINES
                )
            except Exception as exc:  # keep the pages we already have
                note_if_rate_error(exc, self._ex)
                logger.warning("%s OHLCV page failed for %s (%s bars so far): %s",
                               self.exchange_id, symbol, len(rows), exc)
                break
            if not batch:
                break
            fresh = [c for c in batch if not rows or c[0] > rows[-1][0]]
            if not fresh:  # the venue is not advancing — stop rather than spin
                break
            rows.extend(fresh)
            if len(batch) < _MAX_KLINES or len(rows) >= limit:
                break  # reached the end of history, or we have enough
            cursor = int(rows[-1][0]) + tf_ms
        return [_to_candle(c) for c in rows[-limit:]]

    def _timeframe_ms(self, timeframe: str) -> int:
        """Bar duration in ms, or 0 when the exchange cannot tell us (then: no paging)."""
        try:
            return int(self._ex.parse_timeframe(timeframe) * 1000)
        except Exception:
            return 0

    def top_symbols(self, n: int = 10) -> list[str]:
        """Top-N base symbols by quote volume for this exchange's quote asset."""
        try:
            tickers = self._fetch_tickers_map()
        except Exception as exc:
            note_if_rate_error(exc, self._ex)  # 429/418/-1015: note the hold before degrading
            logger.warning("%s fetch_tickers failed: %s", self.exchange_id, exc)
            return []
        rows: list[tuple[str, float]] = []
        suffix = f"/{self.quote}"
        for pair, t in tickers.items():
            if not pair.endswith(suffix):
                continue
            base = pair[: -len(suffix)]
            if _is_excluded_base(base):
                continue
            vol = t.get("quoteVolume") or 0.0
            rows.append((base, float(vol)))
        rows.sort(key=lambda r: r[1], reverse=True)
        return [sym for sym, _ in rows[:n]]

    def all_symbols(self, min_quote_volume: float = 0.0) -> list[str]:
        """All base symbols for this quote whose quote volume clears the floor, by volume desc."""
        try:
            tickers = self._fetch_tickers_map()
        except Exception as exc:
            note_if_rate_error(exc, self._ex)  # 429/418/-1015: note the hold before degrading
            logger.warning("%s fetch_tickers failed: %s", self.exchange_id, exc)
            return []
        rows: list[tuple[str, float]] = []
        suffix = f"/{self.quote}"
        for pair, t in tickers.items():
            if not pair.endswith(suffix):
                continue
            base = pair[: -len(suffix)]
            if _is_excluded_base(base):
                continue
            vol = float(t.get("quoteVolume") or 0.0)
            if vol < min_quote_volume:
                continue
            rows.append((base, vol))
        rows.sort(key=lambda r: r[1], reverse=True)
        return [sym for sym, _ in rows]

    def get_exchange_info(self, symbol: str) -> dict:
        try:
            # ccxt's .market() raises "markets not loaded" until something has called
            # load_markets(). Without this, EVERY call on a cold process fell into the
            # except-branch below and returned _DEFAULT_INFO — i.e. the real LOT_SIZE was
            # never read at all until some other ccxt call happened to warm the cache, and
            # a coin whose true step is 0.1 (KLAY, RVN) got a 1e-05 step and an order the
            # exchange would reject on live. load_markets() is idempotent and ccxt-cached.
            # getattr: test doubles stand in for the ccxt client and don't carry `.markets`;
            # a real ccxt exchange always does, so the load still happens where it matters.
            if not getattr(self._ex, "markets", None) and hasattr(self._ex, "load_markets"):
                self._ex.load_markets()
            market = self._ex.market(self.pair(symbol))
            limits = market.get("limits", {})
            amount = limits.get("amount", {})
            cost = limits.get("cost", {})
            # minQty/minNotional already read the exchange's LOT_SIZE.minQty and
            # NOTIONAL.minNotional correctly — ccxt's normalised `limits.amount.min` /
            # `limits.cost.min` mirror those filters exactly (verified against live
            # Binance for BNB/WBTC/ZEC/BCH/PAXG/BTC/KLAY/RVN). Only stepSize was wrong.
            precision_mode = getattr(self._ex, "precisionMode", None)
            return {
                "symbol": symbol,
                "minQty": amount.get("min") or 0.00001,
                "maxQty": amount.get("max") or 10000.0,
                "stepSize": _step_size_from_market(precision_mode, market) or 0.00001,
                "minNotional": cost.get("min") or 10.0,
            }
        except Exception as exc:
            logger.warning("%s exchange info failed for %s: %s", self.exchange_id, symbol, exc)
            return {**_DEFAULT_INFO, "symbol": symbol}


# --- cached singletons --------------------------------------------------

_providers: dict[str, CcxtProvider] = {}


def get_provider(exchange_id: str) -> CcxtProvider:
    """Return a cached provider for the given ccxt exchange id."""
    if exchange_id not in _providers:
        _providers[exchange_id] = CcxtProvider(exchange_id)
    return _providers[exchange_id]


def live_provider() -> CcxtProvider:
    from app.config import settings

    return get_provider(settings.live_exchange)


def data_provider() -> CcxtProvider:
    from app.config import settings

    return get_provider(settings.data_exchange)


def reset_providers() -> None:
    """Drop cached providers (used in tests)."""
    _providers.clear()
