"""
Pluggable market-data providers built on ccxt (public endpoints only — no API key).

A provider abstracts an exchange so the rest of the app can swap Binance (live
prices) for Kraken / Coinbase (real historical data for backtests and tests)
without code changes. Different exchanges use different quote assets, so each
provider knows its own quote (Binance→USDT, Kraken/Coinbase→USD).

All network failures degrade gracefully (empty list / safe defaults) so a flaky
exchange never crashes a scan.
"""

from __future__ import annotations

import logging
from typing import Protocol, TypedDict

import ccxt

logger = logging.getLogger(__name__)

# ccxt id -> quote asset used for that exchange's USD-ish spot pairs
_QUOTES = {
    "binance": "USDT",
    "binanceus": "USDT",
    "kucoin": "USDT",
    "okx": "USDT",
    "kraken": "USD",
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

    def get_prices(self, symbols: list[str]) -> dict[str, float]: ...
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

    def pair(self, symbol: str) -> str:
        """Map a base symbol (e.g. 'BTC') to this exchange's pair (e.g. 'BTC/USD')."""
        return f"{symbol}/{self.quote}"

    def get_prices(self, symbols: list[str]) -> dict[str, float]:
        """Batch-fetch prices via ``fetch_tickers`` (one call) when the exchange supports it;
        fall back to per-symbol ``fetch_ticker`` when the batched call fails (B9)."""
        if not symbols:
            return {}
        pairs = [self.pair(s) for s in symbols]
        out: dict[str, float] = {}
        try:
            tickers = self._ex.fetch_tickers(pairs)
            for symbol, pair in zip(symbols, pairs, strict=False):
                t = tickers.get(pair) or {}
                last = t.get("last")
                if last is not None:
                    out[symbol] = float(last)
        except Exception:
            # Exchange doesn't support batched fetch_tickers — fall back per-symbol.
            for symbol in symbols:
                try:
                    out[symbol] = float(self._ex.fetch_ticker(self.pair(symbol))["last"])
                except Exception:
                    continue
        return out

    def get_ohlcv(self, symbol: str, timeframe: str = "1d", limit: int = 200) -> list[Candle]:
        try:
            raw = self._ex.fetch_ohlcv(self.pair(symbol), timeframe=timeframe, limit=limit)
        except Exception as exc:
            logger.warning("%s OHLCV failed for %s: %s", self.exchange_id, symbol, exc)
            return []
        return [
            Candle(ts=int(c[0]), open=float(c[1]), high=float(c[2]),
                   low=float(c[3]), close=float(c[4]), volume=float(c[5]))
            for c in raw
        ]

    def top_symbols(self, n: int = 10) -> list[str]:
        """Top-N base symbols by quote volume for this exchange's quote asset."""
        try:
            tickers = self._ex.fetch_tickers()
        except Exception as exc:
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
            tickers = self._ex.fetch_tickers()
        except Exception as exc:
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
