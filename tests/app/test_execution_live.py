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


# --- 1.3: maker placement (post-only entries; risk exits stay taker) ----------

_SOL_MARKET = {
    "info": {"filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.0100"},
        {"filterType": "LOT_SIZE", "stepSize": "0.00100", "minQty": "0.00100"},
        {"filterType": "NOTIONAL", "minNotional": "5.00"},
        {"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "2", "bidMultiplierDown": "0.5"},
    ]},
    "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
}


class _MakerFakeEx:
    """ccxt-like stub that records the full create_order call (incl. params) and can raise."""

    def __init__(self, order=None, raise_exc=None, market=None):
        self._order = order or {}
        self._raise = raise_exc
        self._market = market or {}
        self.calls = []

    def market(self, pair):
        return self._market

    def create_order(self, pair, otype, side, qty, price=None, params=None):
        self.calls.append((pair, otype, side, qty, price, params))
        if self._raise is not None:
            raise self._raise
        return self._order


def test_order_placement_maps_kind_and_params():
    assert execution.order_placement("MARKET", True) == ("market", {})
    assert execution.order_placement("market", True) == ("market", {})   # case-insensitive
    assert execution.order_placement("LIMIT", True) == ("limit", {"postOnly": True})
    assert execution.order_placement("LIMIT", False) == ("limit", {})


def test_is_post_only_reject():
    assert execution.is_post_only_reject(
        Exception('binance {"code":-2010,"msg":"Order would immediately match and take."}')
    )
    assert execution.is_post_only_reject(Exception("post only order would take"))
    assert not execution.is_post_only_reject(Exception("Account has insufficient balance"))
    # The bare -2010 code is ambiguous (also duplicate / insufficient balance) — must NOT match.
    assert not execution.is_post_only_reject(Exception('{"code":-2010,"msg":"Duplicate order sent."}'))


def test_filters_from_market_parses_binance_filters():
    f = execution.filters_from_market(_SOL_MARKET)
    assert f["tickSize"] == 0.01
    assert f["stepSize"] == 0.001
    assert f["minQty"] == 0.001
    assert f["minNotional"] == 5.0
    assert f["percentUp"] == 2.0 and f["percentDown"] == 0.5


def test_filters_from_market_falls_back_to_limits():
    f = execution.filters_from_market(
        {"info": {}, "limits": {"amount": {"min": 0.01}, "cost": {"min": 10.0}}}
    )
    assert f["minQty"] == 0.01 and f["minNotional"] == 10.0


def test_maker_order_is_postonly_and_filter_rounded(monkeypatch):
    fake = _MakerFakeEx(order={"status": "open", "filled": 0.0, "id": "m1"}, market=_SOL_MARKET)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    res = execution.place_live_order("SOL/USDT", "BUY", 0.037190, 142.3372, "LIMIT", maker_orders=True)

    _, otype, _, qty, price, params = fake.calls[0]
    assert otype == "limit"
    assert params == {"postOnly": True}
    assert price == 142.34   # rounded to tickSize
    assert qty == 0.037      # floored to stepSize
    assert res["status"] == "open" and res["quantity"] == 0.0   # resting, not a phantom fill


def test_maker_post_only_reject_returns_rejected(monkeypatch):
    fake = _MakerFakeEx(
        raise_exc=Exception('binance {"code":-2010,"msg":"would immediately match and take"}'),
        market=_SOL_MARKET,
    )
    monkeypatch.setattr(execution, "_client", lambda: fake)
    res = execution.place_live_order("SOL/USDT", "BUY", 0.05, 142.0, "LIMIT", maker_orders=True)
    assert res["status"] == "rejected"
    assert res["quantity"] == 0.0 and res["price"] == 0.0


def test_risk_exit_stays_taker_market_even_with_maker_on(monkeypatch):
    fake = _MakerFakeEx(order={"status": "closed", "filled": 3.0, "amount": 3.0,
                               "average": 50.0, "id": "x"}, market=_SOL_MARKET)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    res = execution.place_live_order("SOL/USDT", "SELL", 3.0, 0.0, "MARKET", maker_orders=True)
    _, otype, _, _, _, params = fake.calls[0]
    assert otype == "market"
    assert params is None        # no postOnly on a risk-exit market order
    assert res["quantity"] == 3.0


def test_maker_reraises_non_post_only_errors(monkeypatch):
    fake = _MakerFakeEx(raise_exc=Exception("Account has insufficient balance"), market=_SOL_MARKET)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    with pytest.raises(Exception, match="insufficient balance"):
        execution.place_live_order("SOL/USDT", "BUY", 0.05, 142.0, "LIMIT", maker_orders=True)


# --- 1.10: idempotent placement via deterministic clientOrderId ---------------


def test_client_order_id_is_deterministic_and_valid():
    assert execution.client_order_id(144) == execution.client_order_id(144)
    assert execution.client_order_id(144) != execution.client_order_id(145)
    cid = execution.client_order_id(144)
    assert cid.startswith("fm-") and len(cid) <= 36


def test_is_duplicate_client_order():
    assert execution.is_duplicate_client_order(Exception('{"code":-2010,"msg":"Duplicate order sent."}'))
    assert not execution.is_duplicate_client_order(Exception("Account has insufficient balance"))


def test_place_live_order_sends_client_order_id(monkeypatch):
    fake = _MakerFakeEx(order={"status": "closed", "filled": 5.0, "amount": 5.0,
                               "average": 100.0, "id": "o1"}, market=_SOL_MARKET)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    execution.place_live_order("SOL/USDT", "BUY", 5.0, 0.0, "MARKET", client_order_id="fm-7")
    _, otype, _, _, _, params = fake.calls[0]
    assert otype == "market"
    assert params == {"clientOrderId": "fm-7"}


class _DupFakeEx(_MakerFakeEx):
    """create_order raises a duplicate-order error; fetch_order returns the recovered order."""

    def __init__(self, recovered):
        super().__init__(
            raise_exc=Exception('{"code":-2010,"msg":"Duplicate order sent."}'), market=_SOL_MARKET
        )
        self._recovered = recovered
        self.fetched = []

    def fetch_order(self, oid, symbol=None, params=None):
        self.fetched.append((oid, symbol, params))
        return self._recovered


def test_duplicate_client_order_recovers_existing_instead_of_double_placing(monkeypatch):
    fake = _DupFakeEx({"status": "closed", "filled": 5.0, "average": 100.0,
                       "fee": {"cost": 0.1}, "id": "EXIST1"})
    monkeypatch.setattr(execution, "_client", lambda: fake)
    res = execution.place_live_order("SOL/USDT", "BUY", 5.0, 0.0, "MARKET", client_order_id="fm-7")

    assert res["raw_id"] == "EXIST1"     # recovered the prior order, not a new placement
    assert res["quantity"] == 5.0 and res["price"] == 100.0
    assert fake.fetched and fake.fetched[0][2] == {"clientOrderId": "fm-7"}
