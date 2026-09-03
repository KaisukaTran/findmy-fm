"""Live-readiness unit tests (paper path untouched): truthful fill, filter compliance,
rate-limit guard. All offline — no network, no real keys."""

import ccxt
import pytest

from app import execution

# --- 1.1: place_live_order reports the TRUTH (no phantom fill) ----------------


class _FakeEx:
    """Minimal ccxt-like stub: create_order returns a preset normalised order dict."""

    def __init__(self, order):
        self._order = order
        self.calls = []

    def create_order(self, pair, otype, side, qty, price=None, params=None):
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


# --- the venue's own fill time (Fix: fills were stamped with reconcile time) ----------
#
# ccxt normalises both `timestamp` and `lastTradeTimestamp` (ms epoch) onto the order dict
# `place_live_order`/`fetch_live_order` get back. Propagated here as `filled_at_ms` so
# `orders._book_delta` can stamp `Fill.executed_at` with the venue's own fill time instead of
# whenever the reconcile pass happened to run.


def test_place_live_order_prefers_last_trade_timestamp(monkeypatch):
    _patch_client(monkeypatch, {"status": "closed", "filled": 5.0, "amount": 5.0,
                                "average": 101.5, "fee": {"cost": 0.05}, "id": "m1",
                                "timestamp": 1000, "lastTradeTimestamp": 2000})
    res = execution.place_live_order("SOL/USDT", "BUY", 5.0, 0.0, "MARKET")
    assert res["filled_at_ms"] == 2000


def test_place_live_order_falls_back_to_timestamp(monkeypatch):
    _patch_client(monkeypatch, {"status": "closed", "filled": 5.0, "amount": 5.0,
                                "average": 101.5, "fee": {"cost": 0.05}, "id": "m1",
                                "timestamp": 1000})
    res = execution.place_live_order("SOL/USDT", "BUY", 5.0, 0.0, "MARKET")
    assert res["filled_at_ms"] == 1000


def test_place_live_order_filled_at_ms_absent_when_venue_reports_neither(monkeypatch):
    _patch_client(monkeypatch, {"status": "closed", "filled": 5.0, "amount": 5.0,
                                "average": 101.5, "fee": {"cost": 0.05}, "id": "m1"})
    res = execution.place_live_order("SOL/USDT", "BUY", 5.0, 0.0, "MARKET")
    assert res.get("filled_at_ms") is None


class _FetchOrderFakeEx:
    """ccxt-like stub: fetch_order returns a preset normalised order dict."""

    def __init__(self, order):
        self._order = order

    def fetch_order(self, order_id, pair, params=None):
        return self._order


def test_fetch_live_order_prefers_last_trade_timestamp(monkeypatch):
    monkeypatch.setattr(execution, "_client", lambda: _FetchOrderFakeEx(
        {"status": "closed", "filled": 5.0, "average": 100.0, "id": "r1",
         "timestamp": 1000, "lastTradeTimestamp": 2000}))
    res = execution.fetch_live_order("SOL/USDT", "r1")
    assert res["filled_at_ms"] == 2000


def test_fetch_live_order_falls_back_to_timestamp(monkeypatch):
    monkeypatch.setattr(execution, "_client", lambda: _FetchOrderFakeEx(
        {"status": "closed", "filled": 5.0, "average": 100.0, "id": "r1", "timestamp": 1000}))
    res = execution.fetch_live_order("SOL/USDT", "r1")
    assert res["filled_at_ms"] == 1000


def test_fetch_live_order_filled_at_ms_absent_when_venue_reports_neither(monkeypatch):
    monkeypatch.setattr(execution, "_client", lambda: _FetchOrderFakeEx(
        {"status": "closed", "filled": 5.0, "average": 100.0, "id": "r1"}))
    res = execution.fetch_live_order("SOL/USDT", "r1")
    assert res.get("filled_at_ms") is None


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
    # P2 spec change (2026-09-01): the old 5.0s ceiling guaranteed re-hitting a REQUEST_WEIGHT
    # budget that had already been reached — a minute-window budget needs up to a minute to
    # roll. Raised to 30.0; see the docstring on weight_backoff_seconds for the full rationale.
    assert execution.weight_backoff_seconds(6000) == 30.0         # at limit → hard backoff
    assert execution.weight_backoff_seconds(7000) == 30.0         # over limit


def test_classify_rate_error():
    """Keyed on the HTTP STATUS, not a substring of the message: a Binance signed URL carries
    an orderId and a 13-digit timestamp, so ~3% of ordinary network errors contain "418"
    somewhere and were being read as an IP ban."""
    import ccxt

    def _err(cls, msg, status=None):
        exc = cls(msg)
        if status is not None:
            exc.http_status_code = status
        return exc

    assert execution.classify_rate_error(_err(ccxt.DDoSProtection, "429 too many", 429)) == ("retry", 1.0)
    assert execution.classify_rate_error(
        _err(ccxt.DDoSProtection, "HTTP 429", 429), retry_after=3.0) == ("retry", 3.0)
    assert execution.classify_rate_error(_err(ccxt.DDoSProtection, "418 IP banned", 418)) == ("halt", None)
    assert execution.classify_rate_error(Exception("some other error")) == ("raise", None)
    # A plain timeout whose URL happens to contain 418 is NOT a ban.
    assert execution.classify_rate_error(
        ccxt.RequestTimeout("binance GET .../order?orderId=4180192837 read timeout")
    ) == ("raise", None)


# --- P2 Fix B1: retry_after_seconds / note_if_rate_error / rate_hold_active --------------


def test_retry_after_seconds_reads_the_exchange_last_response_headers():
    class _Ex:
        last_response_headers = {"Retry-After": "17"}

    assert execution.retry_after_seconds(Exception("429 too many"), _Ex()) == 17.0


def test_retry_after_seconds_header_lookup_is_case_insensitive():
    class _Ex:
        last_response_headers = {"retry-after": "5"}

    assert execution.retry_after_seconds(Exception("x"), _Ex()) == 5.0


def test_retry_after_seconds_falls_back_to_the_exception_text():
    exc = Exception("binance 429 too many requests, Retry-After: 42")

    assert execution.retry_after_seconds(exc) == 42.0


def test_retry_after_seconds_is_none_when_nothing_usable_is_found():
    assert execution.retry_after_seconds(Exception("boring error")) is None
    assert execution.retry_after_seconds(Exception("boring error"), object()) is None


def test_note_if_rate_error_notes_and_returns_true_for_a_rate_classified_error():
    execution.reset_client_cache()
    try:
        exc = ccxt.DDoSProtection("binance 429 too many")
        exc.http_status_code = 429

        assert execution.note_if_rate_error(exc) is True
        assert execution.rate_limited_until() > 0
    finally:
        execution.reset_client_cache()


def test_note_if_rate_error_is_a_noop_for_a_non_rate_error():
    execution.reset_client_cache()
    try:
        assert execution.note_if_rate_error(Exception("some other error")) is False
        assert execution.rate_limited_until() == 0
    finally:
        execution.reset_client_cache()


def test_rate_hold_active_reflects_either_hold():
    execution.reset_client_cache()
    try:
        assert execution.rate_hold_active() is False

        exc = ccxt.DDoSProtection("binance 418 banned")
        exc.http_status_code = 418
        execution.note_rate_error(exc)

        assert execution.rate_hold_active() is True
    finally:
        execution.reset_client_cache()


def test_rate_hold_active_reflects_a_weight_hold_too():
    execution.reset_client_cache()
    try:
        execution._note_weight_usage(type("Ex", (), {"last_response_headers":
                                                       {"X-MBX-USED-WEIGHT-1M": "6000"}})())

        assert execution.rate_hold_active() is True
    finally:
        execution.reset_client_cache()


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
    stp = {"selfTradePreventionMode": execution.SELF_TRADE_PREVENTION["SELL"]}
    assert execution.order_placement("MARKET", True) == ("market", stp)
    assert execution.order_placement("market", True) == ("market", stp)   # case-insensitive
    assert execution.order_placement("LIMIT", True) == ("limit", {**stp, "postOnly": True})
    assert execution.order_placement("LIMIT", False) == ("limit", stp)


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
    assert params["postOnly"] is True
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
    assert "postOnly" not in (params or {})   # no postOnly on a risk-exit market order
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
    assert params["clientOrderId"] == "fm-7"


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


# --- A7 (1.10): fetch_order_by_client_id — the probe used before a retry --------------
#
# conftest.py defaults `execution.fetch_order_by_client_id` to a stub (None) for every OTHER
# test, so a test faking `live_enabled()` never falls through to a real network call without
# meaning to. These tests exercise the REAL function, so they call the reference captured at
# import time — before that per-test stub ever applies.

_real_fetch_order_by_client_id = execution.fetch_order_by_client_id


def test_fetch_order_by_client_id_returns_the_normalised_shape(monkeypatch):
    fake = _DupFakeEx({"status": "closed", "filled": 5.0, "average": 100.0,
                       "fee": {"cost": 0.1}, "id": "EXIST1"})
    monkeypatch.setattr(execution, "_client", lambda: fake)
    res = _real_fetch_order_by_client_id("SOL/USDT", "fm-7")
    assert res == {"price": 100.0, "quantity": 5.0, "fee": 0.1, "fee_base": 0.0,
                   "raw_id": "EXIST1", "status": "closed"}


def test_fetch_order_by_client_id_closed_without_filled_trusts_amount(monkeypatch):
    """Fix round A / item 1 (execution.py half): same fallback as `place_live_order` — a
    fully-filled order whose venue response omitted `filled` must not be adopted as a phantom
    zero-fill (price 0), which an A7 adoption would otherwise reject with 'no fill price'."""
    fake = _DupFakeEx({"status": "closed", "amount": 5.0, "average": 100.0,
                       "fee": {"cost": 0.1}, "id": "EXIST2"})
    monkeypatch.setattr(execution, "_client", lambda: fake)
    res = _real_fetch_order_by_client_id("SOL/USDT", "fm-8")
    assert res["quantity"] == 5.0
    assert res["price"] == 100.0


class _NotFoundFakeEx:
    def __init__(self, exc):
        self._exc = exc

    def fetch_order(self, oid, symbol=None, params=None):
        raise self._exc


def test_fetch_order_by_client_id_returns_none_when_the_venue_never_saw_it(monkeypatch):
    fake = _NotFoundFakeEx(ccxt.OrderNotFound('binance {"code":-2013,"msg":"Order does not exist."}'))
    monkeypatch.setattr(execution, "_client", lambda: fake)
    assert _real_fetch_order_by_client_id("SOL/USDT", "fm-9") is None


def test_fetch_order_by_client_id_reraises_a_non_gone_error(monkeypatch):
    fake = _NotFoundFakeEx(Exception("Account has insufficient balance"))
    monkeypatch.setattr(execution, "_client", lambda: fake)
    with pytest.raises(Exception, match="insufficient balance"):
        _real_fetch_order_by_client_id("SOL/USDT", "fm-9")


# --- B5 (minNotional on a price-less MARKET order) + B6 (a real PERCENT_PRICE reference) --

_PERCENT_MARKET = {
    "info": {"filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
        {"filterType": "NOTIONAL", "minNotional": "5.00"},
        # A realistic Binance PERCENT_PRICE_BY_SIDE band (~5%), not the literal number 5.
        {"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "1.05", "bidMultiplierDown": "0.95"},
    ]},
    "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
}


def test_market_buy_under_minnotional_valued_at_market_price_is_rejected(monkeypatch):
    """px<=0 (MARKET) carries no price of its own — B5 values it at the CURRENT market price
    instead of silently popping minNotional (which used to let it sail through to the venue
    and be rejected there instead)."""
    from app import market

    fake = _MakerFakeEx(market=_PERCENT_MARKET)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"SOL": 100.0})
    # 0.03 * 100.0 = 3.0, below the 5.0 minNotional.
    with pytest.raises(ValueError, match="minNotional"):
        execution.place_live_order("SOL/USDT", "BUY", 0.03, 0.0, "MARKET")
    assert fake.calls == [], "rejected before ever reaching the venue"


def test_market_sell_under_minnotional_is_never_gated(monkeypatch):
    """The exact same order as a SELL must never be blocked by this check — exits are never
    gated; the venue is the right place to refuse a dust position."""
    from app import market

    fake = _MakerFakeEx(order={"status": "closed", "filled": 0.03, "amount": 0.03,
                               "average": 100.0, "id": "s1"}, market=_PERCENT_MARKET)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"SOL": 100.0})
    res = execution.place_live_order("SOL/USDT", "SELL", 0.03, 0.0, "MARKET")
    assert res["quantity"] == 0.03


def test_market_buy_with_no_market_price_available_is_not_blocked(monkeypatch):
    """No cached/live price (offline, cold cache) degrades to today's behaviour: skip the
    check rather than block a BUY on missing data."""
    from app import market

    fake = _MakerFakeEx(order={"status": "closed", "filled": 0.03, "amount": 0.03,
                               "average": 100.0, "id": "b1"}, market=_PERCENT_MARKET)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {})
    res = execution.place_live_order("SOL/USDT", "BUY", 0.03, 0.0, "MARKET")
    assert res["quantity"] == 0.03


def test_buy_limit_priced_far_above_market_hits_the_real_percent_price_band(monkeypatch):
    """B6: the reference used to be the order's OWN price (px if px>0 else None) — comparing a
    number with itself can never fire. It is now the REAL current market price."""
    from app import market

    fake = _MakerFakeEx(market=_PERCENT_MARKET)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"SOL": 100.0})
    with pytest.raises(ValueError, match="PERCENT_PRICE"):
        execution.place_live_order("SOL/USDT", "BUY", 1.0, 300.0, "LIMIT")  # 3x market
    assert fake.calls == []


def test_sell_limit_far_from_market_is_never_gated_by_percent_price(monkeypatch):
    """A SELL outside the band is the venue's problem — never gated by us."""
    from app import market

    fake = _MakerFakeEx(order={"status": "open", "filled": 0.0, "id": "sp1"}, market=_PERCENT_MARKET)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"SOL": 100.0})
    res = execution.place_live_order("SOL/USDT", "SELL", 1.0, 300.0, "LIMIT")
    assert res["status"] == "open"


def test_buy_limit_with_no_market_price_is_not_blocked_by_percent_price(monkeypatch):
    from app import market

    fake = _MakerFakeEx(order={"status": "open", "filled": 0.0, "id": "bp1"}, market=_PERCENT_MARKET)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {})
    res = execution.place_live_order("SOL/USDT", "BUY", 1.0, 300.0, "LIMIT")
    assert res["status"] == "open"


# --- Fix round A / item 5(d): the B5/B6 lookup + minNotional-check reordering --------------


def test_market_buy_skips_the_price_lookup_during_a_rate_hold(monkeypatch):
    """The market-price lookup (for the B5/B6 checks) used to run BEFORE the rate gates below,
    re-hammering a banned IP once per queued BUY per cycle. During a hold it must degrade
    straight to mkt_price=None instead — the checks that need it just skip."""
    from app import market

    fake = _MakerFakeEx(order={"status": "closed", "filled": 1.0, "amount": 1.0,
                               "average": 100.0, "id": "rh1"}, market=_PERCENT_MARKET)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    monkeypatch.setattr(execution, "rate_hold_active", lambda: True)
    calls: list = []
    monkeypatch.setattr(market, "get_current_prices",
                        lambda syms: calls.append(syms) or {"SOL": 100.0})

    res = execution.place_live_order("SOL/USDT", "BUY", 1.0, 0.0, "MARKET")

    assert calls == [], "must not call get_current_prices while a rate hold is active"
    assert res["quantity"] == 1.0


def test_market_buy_minnotional_checked_after_rounding_not_before(monkeypatch):
    """The manual minNotional check must run on the ROUNDED (adj_qty) amount, not the raw
    requested one. `round_to_filters` floors qty DOWN to stepSize, so a qty that clears the $5
    floor BEFORE rounding can land under it AFTER — checking the pre-round quantity let such an
    order sail through our own validation and reach the venue anyway."""
    from app import market

    filters = {
        "info": {"filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.02", "minQty": "0.001"},
            {"filterType": "NOTIONAL", "minNotional": "5.00"},
        ]},
        "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
    }
    fake = _MakerFakeEx(order={"status": "closed", "filled": 0.04, "amount": 0.04,
                               "average": 100.0, "id": "b2"}, market=filters)
    monkeypatch.setattr(execution, "_client", lambda: fake)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"SOL": 100.0})
    # Unrounded: 0.059 * 100 = 5.9 (clears the $5 floor). Floored to stepSize 0.02 -> 0.04,
    # whose REAL notional (0.04 * 100 = 4.0) does NOT clear it.
    with pytest.raises(ValueError, match="minNotional"):
        execution.place_live_order("SOL/USDT", "BUY", 0.059, 0.0, "MARKET")
    assert fake.calls == [], "must be rejected before ever reaching the venue"
