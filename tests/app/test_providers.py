"""Tests for app.data.providers (ccxt wrapper) with a fake offline exchange."""

from app.data.providers import CcxtProvider


class _FakeEx:
    _prices = {"BTC/USD": 65000.0, "ETH/USD": 3500.0}

    def fetch_ticker(self, pair):
        return {"last": self._prices[pair]}

    def fetch_ohlcv(self, pair, timeframe="1d", limit=200):
        base = 1_000_000_000_000
        return [[base + i * 86_400_000, 100 + i, 101 + i, 99 + i, 100 + i, 5.0] for i in range(limit)]

    def fetch_tickers(self):
        return {
            "BTC/USD": {"quoteVolume": 1e9},
            "ETH/USD": {"quoteVolume": 5e8},
            "DOGE/USDT": {"quoteVolume": 9e9},  # wrong quote for a USD exchange -> excluded
        }

    def market(self, pair):
        return {"limits": {"amount": {"min": 0.0001, "max": 1000.0}, "cost": {"min": 5.0}},
                "precision": {"amount": 0.0001}}


def _provider():
    p = CcxtProvider("kraken")  # ccxt.kraken() constructs offline; no network until a fetch
    p._ex = _FakeEx()
    return p


def test_pair_uses_exchange_quote():
    assert _provider().pair("BTC") == "BTC/USD"


def test_get_prices():
    assert _provider().get_prices(["BTC", "ETH"]) == {"BTC": 65000.0, "ETH": 3500.0}


def test_get_ohlcv_shape():
    candles = _provider().get_ohlcv("BTC", limit=10)
    assert len(candles) == 10
    assert candles[0]["close"] == 100 and candles[9]["close"] == 109
    assert set(candles[0].keys()) == {"ts", "open", "high", "low", "close", "volume"}


def test_top_symbols_filters_quote_and_sorts():
    top = _provider().top_symbols(5)
    assert top == ["BTC", "ETH"]  # DOGE/USDT excluded for a USD-quote exchange


def test_exchange_info():
    info = _provider().get_exchange_info("BTC")
    assert info["minQty"] == 0.0001 and info["minNotional"] == 5.0
