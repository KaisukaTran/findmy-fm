"""
Phase 1 USDT-correctness regression tests.

Asserts that:
- Every Binance-family exchange in the provider quote map uses USDT, not USD.
- CcxtProvider("binance") builds pairs with /USDT suffix (e.g. BTC/USDT).
- Every base symbol in the default watchlist produces a USDT pair on Binance.
- A legacy bare-USD pair string (e.g. "BTCUSD") is NOT what the Binance provider
  produces; if someone reintroduces a USD quote for Binance these tests fail.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.data.providers import _QUOTES, CcxtProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeBinanceEx:
    """Minimal stub — no network, just enough to construct a provider."""

    def market(self, pair: str) -> dict:  # noqa: ARG002
        return {
            "limits": {"amount": {"min": 0.0001, "max": 10000.0}, "cost": {"min": 10.0}},
            "precision": {"amount": 0.00001},
        }


def _binance_provider() -> CcxtProvider:
    """Build a CcxtProvider for Binance with the live exchange swapped for the stub."""
    p = CcxtProvider("binance")
    p._ex = _FakeBinanceEx()
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_binance_quote_in_registry_is_usdt():
    """_QUOTES must map every Binance-family exchange to USDT, never USD."""
    binance_family = [k for k in _QUOTES if "binance" in k]
    assert binance_family, "Expected at least one binance* key in _QUOTES"
    for key in binance_family:
        assert _QUOTES[key] == "USDT", (
            f"_QUOTES['{key}'] = {_QUOTES[key]!r} — must be 'USDT', not 'USD'. "
            "All Binance trading pairs are quoted in USDT."
        )


def test_binance_provider_pair_ends_with_usdt():
    """CcxtProvider('binance').pair('BTC') must return 'BTC/USDT', not 'BTC/USD'."""
    p = _binance_provider()
    result = p.pair("BTC")
    assert result == "BTC/USDT", (
        f"pair('BTC') returned {result!r}; expected 'BTC/USDT'. "
        "Binance uses USDT as the quote asset, not USD."
    )
    assert not result.endswith("/USD"), "pair must not end with bare /USD for Binance"


@pytest.mark.parametrize("symbol", ["BTC", "ETH", "SOL"])
def test_default_watchlist_symbols_produce_usdt_pairs(symbol: str):
    """Every default watchlist symbol produces a USDT pair on Binance."""
    p = _binance_provider()
    pair = p.pair(symbol)
    assert pair.endswith("/USDT"), (
        f"Binance pair for {symbol!r} is {pair!r}; must end with '/USDT'."
    )


def test_legacy_usd_pair_not_produced_by_binance():
    """The Binance provider must never emit a bare-USD pair string like 'BTC/USD'."""
    p = _binance_provider()
    for symbol in settings.watchlist:
        pair = p.pair(symbol)
        assert "/USD" not in pair or pair.endswith("/USDT"), (
            f"Binance pair {pair!r} contains '/USD' without USDT suffix — "
            "this indicates a regression where Binance was assigned USD as its quote."
        )


def test_non_usdt_pair_string_is_not_binance_format():
    """
    Guard: a hardcoded 'BTCUSD' string (no slash, no T) is not what CcxtProvider
    produces for Binance. If this test fails it means the provider format changed.
    """
    p = _binance_provider()
    pair = p.pair("BTC")
    assert pair != "BTCUSD", (
        "Provider returned old-style 'BTCUSD' pair — Binance requires 'BTC/USDT'."
    )
    assert pair != "BTC/USD", (
        "Provider returned 'BTC/USD' for Binance — must be 'BTC/USDT'."
    )


def test_kucoin_and_okx_also_use_usdt():
    """Sanity-check: other USDT exchanges in the registry are not accidentally changed to USD."""
    for exchange in ("kucoin", "okx"):
        if exchange in _QUOTES:
            assert _QUOTES[exchange] == "USDT", (
                f"_QUOTES['{exchange}'] should be 'USDT'; found {_QUOTES[exchange]!r}."
            )


def test_usd_exchanges_correctly_use_usd():
    """Kraken/Coinbase legitimately use USD — confirm they are NOT accidentally changed to USDT."""
    usd_exchanges = {k: v for k, v in _QUOTES.items() if "binance" not in k and "kucoin" not in k and "okx" not in k}
    for exchange, quote in usd_exchanges.items():
        assert quote == "USD", (
            f"Non-Binance exchange {exchange!r} unexpectedly has quote={quote!r}; "
            "Kraken/Coinbase/Bitstamp use real USD and should stay USD."
        )
