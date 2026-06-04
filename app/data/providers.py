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
        self._ex = getattr(ccxt, exchange_id)()

    def pair(self, symbol: str) -> str:
        """Map a base symbol (e.g. 'BTC') to this exchange's pair (e.g. 'BTC/USD')."""
        return f"{symbol}/{self.quote}"

    def get_prices(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for symbol in symbols:
            try:
                out[symbol] = float(self._ex.fetch_ticker(self.pair(symbol))["last"])
            except Exception:  # one bad symbol shouldn't fail the batch
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
            vol = t.get("quoteVolume") or 0.0
            rows.append((pair[: -len(suffix)], float(vol)))
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
            vol = float(t.get("quoteVolume") or 0.0)
            if vol < min_quote_volume:
                continue
            rows.append((pair[: -len(suffix)], vol))
        rows.sort(key=lambda r: r[1], reverse=True)
        return [sym for sym, _ in rows]

    def get_exchange_info(self, symbol: str) -> dict:
        try:
            market = self._ex.market(self.pair(symbol))
            limits = market.get("limits", {})
            amount = limits.get("amount", {})
            cost = limits.get("cost", {})
            return {
                "symbol": symbol,
                "minQty": amount.get("min") or 0.00001,
                "maxQty": amount.get("max") or 10000.0,
                "stepSize": market.get("precision", {}).get("amount") or 0.00001,
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
