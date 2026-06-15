"""Live-readiness unit tests (paper path untouched): truthful fill, filter compliance,
rate-limit guard. All offline — no network, no real keys."""

import pytest

from app import execution

# --- 1.1: place_live_order reports the TRUTH (no phantom fill) ----------------


class _FakeEx:
    """Minimal ccxt-like stub: create_order returns a preset normalised order dict."""

    def __init__(self, order):
        self._order = order
        self.calls = []

    def create_order(self, pair, otype, side, qty, price=None):
        self.calls.append((pair, otype, side, qty, price))
        return self._order


def _patch_client(monkeypatch, order):
    fake = _FakeEx(order)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    return fake


def test_resting_maker_order_is_not_a_phantom_fill(monkeypatch):
    # A post-only order that rests on the book: status open, filled 0.
    _patch_client(monkeypatch, {"status": "open", "filled": 0.0, "amount": 5.0,
                                "average": None, "price": 100.0, "id": "abc"})
    res = execution.place_live_order("SOL/USDT", "BUY", 5.0, 100.0, "LIMIT")
    assert res["quantity"] == 0.0, "resting order must report filled=0, not the amount"
    assert res["price"] == 0.0
    assert res["status"] == "open"
    assert res["raw_id"] == "abc"


def test_filled_market_order_reports_real_fill(monkeypatch):
    _patch_client(monkeypatch, {"status": "closed", "filled": 5.0, "amount": 5.0,
                                "average": 101.5, "fee": {"cost": 0.05}, "id": "m1"})
    res = execution.place_live_order("SOL/USDT", "BUY", 5.0, 0.0, "MARKET")
    assert res["quantity"] == 5.0
    assert res["price"] == 101.5
    assert res["fee"] == 0.05


def test_partial_fill_reports_partial_qty(monkeypatch):
    _patch_client(monkeypatch, {"status": "open", "filled": 2.0, "amount": 5.0,
                                "average": 100.0, "id": "p1"})
    res = execution.place_live_order("SOL/USDT", "BUY", 5.0, 100.0, "LIMIT")
    assert res["quantity"] == 2.0
    assert res["price"] == 100.0


def test_closed_without_filled_field_trusts_amount(monkeypatch):
    # Some venues omit `filled` on a fully-filled order; status=closed → trust amount.
    _patch_client(monkeypatch, {"status": "closed", "amount": 3.0, "average": 50.0, "id": "c1"})
    res = execution.place_live_order("SOL/USDT", "SELL", 3.0, 0.0, "MARKET")
    assert res["quantity"] == 3.0
    assert res["price"] == 50.0


# --- 1.2: exchange-filter compliance (SOLUSDT-style filters) ------------------

_SOL = {"tickSize": 0.01, "stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0}


def test_round_to_filters_rounds_price_and_floors_qty():
    price, qty = execution.round_to_filters(142.3372, 0.037190, _SOL)
    assert price == 142.34          # rounded to tickSize 0.01
    assert qty == 0.037             # floored to stepSize 0.001


def test_round_to_filters_rejects_below_min_notional():
    with pytest.raises(ValueError, match="minNotional"):
        execution.round_to_filters(100.0, 0.001, _SOL)  # 0.10 << $5


def test_round_to_filters_rejects_below_min_qty():
    with pytest.raises(ValueError, match="minQty"):
        execution.round_to_filters(100.0, 0.0004, _SOL)  # floors to 0 < minQty


def test_round_to_filters_enforces_percent_price_band():
    f = {**_SOL, "percentUp": 2.0, "percentDown": 0.5}
    # ref 100 → band [50, 200]; 250 is too high, 40 too low.
    with pytest.raises(ValueError, match="PERCENT_PRICE cap"):
        execution.round_to_filters(250.0, 1.0, f, ref_price=100.0)
    with pytest.raises(ValueError, match="PERCENT_PRICE floor"):
        execution.round_to_filters(40.0, 1.0, f, ref_price=100.0)
    # inside the band is fine
    p, q = execution.round_to_filters(150.0, 1.0, f, ref_price=100.0)
    assert p == 150.0 and q == 1.0


# --- 1.6: rate-limit guard ---------------------------------------------------


def test_used_weight_from_headers_case_insensitive():
    assert execution.used_weight_from_headers({"X-MBX-USED-WEIGHT-1M": "4200"}) == 4200
    assert execution.used_weight_from_headers({"x-mbx-used-weight-1m": "10"}) == 10
    assert execution.used_weight_from_headers({"other": "1"}) is None
    assert execution.used_weight_from_headers(None) is None


def test_weight_backoff_thresholds():
    assert execution.weight_backoff_seconds(0) == 0.0
    assert execution.weight_backoff_seconds(4000) == 0.0          # below 80% of 6000 (4800)
    assert execution.weight_backoff_seconds(4800) == 0.0          # exactly at soft threshold
    assert execution.weight_backoff_seconds(5400) > 0.0           # ramping
    assert execution.weight_backoff_seconds(6000) == 5.0          # at limit → hard backoff
    assert execution.weight_backoff_seconds(7000) == 5.0          # over limit


def test_classify_rate_error():
    assert execution.classify_rate_error(Exception("binance 429 too many")) == ("retry", 1.0)
    assert execution.classify_rate_error(Exception("HTTP 429"), retry_after=3.0) == ("retry", 3.0)
    assert execution.classify_rate_error(Exception("418 IP banned")) == ("halt", None)
    assert execution.classify_rate_error(Exception("some other error")) == ("raise", None)
