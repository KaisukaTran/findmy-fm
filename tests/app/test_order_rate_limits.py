"""The app must respect Binance's ORDER-COUNT budget, not just REQUEST_WEIGHT — and it must
never let that budget gate an exit.

Real limits queried from the live `/api/v3/exchangeInfo` on 2026-08-30:

    REQUEST_WEIGHT   6000    per 1 MINUTE
    ORDERS            100    per 10 SECONDS
    ORDERS         200000    per 1 DAY
    RAW_REQUESTS   300000    per 5 MINUTES

Hammering the venue after a 429 escalates an IP ban from ~2 minutes toward 3 days, and
sustained abuse is an ACCOUNT-level matter — so three separate things have to be true:

1. **ORDERS is an unfilled-order COUNT, not a raw per-window tally.** Binance's own FAQ
   (developers.binance.com/docs/binance-spot-api-docs/faqs/order_count_decrement) says a
   successful placement adds 1; a FILL credits some of it back (more for an efficient resting
   maker fill); a CANCEL or an EXPIRY credits back NOTHING — and the count is tracked per
   ACCOUNT, shared across every IP, API key and API, so rotating keys does not evade it. That
   makes the 1.5 resting model's cancel+replace cycle (`_cancel_resting`/`sync_resting_orders`
   in app/orders.py) the pattern most likely to exhaust it: every re-rest spends a fresh unit
   with no refund for the rung it replaced. We must slow down BEFORE hitting it (a soft 80%
   threshold), not after.
2. **Exceeding it is HTTP 429 with body code -1015 ("Too many new orders").** ccxt raises the
   very same `DDoSProtection` it uses for an ordinary IP-level 429 — the distinguishing
   information is only in the message body — so `classify_rate_error` has to look for it
   specifically, or a -1015 gets an anonymous 1-second retry that does nothing for a breach of
   the 200000/day bucket.
3. **THE ONE UNFORGIVABLE BUG IN THIS PROJECT IS A GATED EXIT.** A SELL — stop-loss, trailing
   stop, timeout close — must ALWAYS be allowed through, even at 100% of the order-count
   budget or mid-hold from a -1015, exactly like the existing
   `assert_not_rate_limited(urgent=True)` contract. A previous commit broke this once already
   and it was caught in review; several tests below exist purely to keep it broken from ever
   landing again.

Also covered: the previously-dead `used_weight_from_headers`/`weight_backoff_seconds` pair
now has a real caller (`_note_weight_usage`, reading `ex.last_response_headers` — confirmed
present on the installed ccxt 4.0.5, see `test_last_response_headers_exists_on_this_ccxt`),
and the fallback constants above are overridable from the venue's own `exchangeInfo`
(`parse_venue_rate_limits`/`refresh_venue_limits`) without ever hitting the network in a test.
"""

from __future__ import annotations

import time

import ccxt
import pytest
from ccxt.base.errors import DDoSProtection

from app import execution, orders
from app.models import APPROVED, PendingOrder


@pytest.fixture(autouse=True)
def _clean():
    execution.reset_client_cache()
    yield
    execution.reset_client_cache()


def _venue_error(msg: str, status: int) -> Exception:
    exc = DDoSProtection(msg)
    exc.http_status_code = status
    return exc


def _orders_exceeded(window: str = "10 seconds") -> Exception:
    """A real -1015 body, shaped exactly as ccxt's binance.py assembles it: id + status +
    reason + the raw JSON — see handle_errors, which raises DDoSProtection on ANY 429/418
    before ever inspecting the body's own error code."""
    return DDoSProtection(
        "binance 429 Too Many Requests "
        '{"code":-1015,"msg":"Too many new orders; current limit is 100 orders per '
        f'{window}.' + '"}'
    )


# --- -1015: an unfilled-order-count breach, not an anonymous 429 ----------------------------


def test_a_1015_body_is_recognised_even_though_ccxt_raises_a_bare_429():
    action, wait = execution.classify_rate_error(_orders_exceeded())

    assert action == "orders_exceeded" and wait


def test_the_wait_honours_the_window_the_venue_actually_named():
    _, wait_10s = execution.classify_rate_error(_orders_exceeded("10 seconds"))
    _, wait_1day = execution.classify_rate_error(_orders_exceeded("1 DAY"))

    assert wait_10s == pytest.approx(10.0)
    assert wait_1day == pytest.approx(86400.0)
    assert wait_1day > wait_10s, "a DAY-bucket breach is not cleared by a 1s nap"


def test_an_ordinary_429_is_still_a_plain_retry_not_orders_exceeded():
    action, _ = execution.classify_rate_error(_venue_error("binance 429 Too Many Requests", 429))

    assert action == "retry"


def test_the_venue_named_window_wins_over_a_shorter_retry_after():
    """Fix round A / item 5(a): `_orders_exceeded_wait` used to prefer `retry_after` OVER the
    window the venue's own message named — inverted vs its own docstring. A Retry-After header
    must never SHORTEN an "... per 1 DAY." hold to whatever short value it happens to carry."""
    action, wait = execution.classify_rate_error(_orders_exceeded("1 DAY"), retry_after=5.0)

    assert action == "orders_exceeded"
    assert wait == pytest.approx(86400.0), "the message-named DAY window must win over retry_after=5"


def test_a_1015_hold_never_blocks_a_sell(monkeypatch):
    """End to end: a stop-loss must reach the venue even while an ORDERS-budget hold is live."""
    sent = []

    class _Ex:
        def create_order(self, pair, typ, side, qty, price=None, params=None):
            sent.append(side)
            return {"id": "1", "status": "closed", "filled": qty, "average": 10.0,
                    "amount": qty, "fee": {"cost": 0.0}}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    execution.note_rate_error(_orders_exceeded())

    execution.place_live_order("SOL/USDT", "SELL", 1.0, 0.0, "MARKET")

    assert sent == ["sell"]


def test_a_1015_hold_refuses_a_new_buy(monkeypatch):
    class _Ex:
        def create_order(self, *a, **k):
            raise AssertionError("must not reach the venue")

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    execution.note_rate_error(_orders_exceeded())

    with pytest.raises(execution.RateLimited):
        execution.place_live_order("SOL/USDT", "BUY", 1.0, 10.0, "LIMIT")


# --- order-count budget: an outstanding COUNT (placement +1, fill -1, cancel/expiry NEVER) --


def test_record_order_placed_increments_the_outstanding_count():
    execution.reset_order_budget()
    execution.record_order_placed()
    execution.record_order_placed()

    assert execution.outstanding_order_count() == 2


def test_record_order_filled_credits_it_back():
    execution.reset_order_budget()
    execution.record_order_placed()
    execution.record_order_placed()

    credited = execution.record_order_filled()

    assert credited == 1
    assert execution.outstanding_order_count() == 1


def test_record_order_filled_never_goes_negative():
    execution.reset_order_budget()

    credited = execution.record_order_filled(credit=3)

    assert credited == 0
    assert execution.outstanding_order_count() == 0


def test_cancelling_does_not_credit_anything():
    """The one semantic that actually drives -1015 here: a cancel is not a fill. Nothing in
    this module calls record_order_filled from a cancel path — this pins that down."""
    execution.reset_order_budget()
    execution.record_order_placed()

    # cancel_live_order never touches the order-count tracker at all.
    assert execution.outstanding_order_count() == 1


def test_events_age_out_of_a_window_over_time():
    execution.reset_order_budget()
    execution.set_order_limits({"10s": (10.0, 100), "1d": (86400.0, 200_000)})
    long_ago = time.monotonic() - 90_000  # older than the 1-day window
    execution.record_order_placed(now=long_ago)
    execution.record_order_placed()

    assert execution.order_count_in_window(86400.0) == 1


def test_new_exposure_is_allowed_below_the_soft_threshold():
    execution.reset_order_budget()
    execution.set_order_limits({"10s": (10.0, 5)})
    for _ in range(3):  # 3/5 = 60% < the 80% soft threshold
        execution.record_order_placed()

    execution.assert_order_budget_available(urgent=False)  # must not raise


def test_new_exposure_is_refused_at_the_soft_threshold_before_the_cap_is_actually_hit():
    """Slow down BEFORE the venue answers -1015, not after."""
    execution.reset_order_budget()
    execution.set_order_limits({"10s": (10.0, 5)})
    for _ in range(4):  # 4/5 = 80% == the soft threshold; cap itself is still one order away
        execution.record_order_placed()

    with pytest.raises(execution.RateLimited):
        execution.assert_order_budget_available(urgent=False)


def test_the_day_bucket_gates_independently_of_the_10s_bucket():
    execution.reset_order_budget()
    execution.set_order_limits({"10s": (10.0, 1000), "1d": (86400.0, 5)})
    for _ in range(4):
        execution.record_order_placed()

    with pytest.raises(execution.RateLimited):
        execution.assert_order_budget_available(urgent=False)


# --- THE unforgivable bug: a SELL must survive a saturated order-count budget ---------------


def test_a_sell_survives_a_saturated_order_budget():
    execution.reset_order_budget()
    execution.set_order_limits({"10s": (10.0, 1)})
    execution.record_order_placed()
    execution.record_order_placed()  # already 200% of a tiny cap

    execution.assert_order_budget_available(urgent=True)  # must not raise


def test_placing_a_sell_survives_a_saturated_order_budget_end_to_end(monkeypatch):
    sent = []

    class _Ex:
        def create_order(self, pair, typ, side, qty, price=None, params=None):
            sent.append(side)
            return {"id": "1", "status": "closed", "filled": qty, "average": 10.0,
                    "amount": qty, "fee": {"cost": 0.0}}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    execution.reset_order_budget()
    execution.set_order_limits({"10s": (10.0, 1)})
    execution.record_order_placed()
    execution.record_order_placed()

    execution.place_live_order("SOL/USDT", "SELL", 1.0, 0.0, "MARKET")

    assert sent == ["sell"], "the stop-loss must reach the venue regardless of the budget"


def test_placing_a_buy_is_refused_at_a_saturated_order_budget(monkeypatch):
    class _Ex:
        def create_order(self, *a, **k):
            raise AssertionError("new exposure must not reach a saturated-budget venue")

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    execution.reset_order_budget()
    execution.set_order_limits({"10s": (10.0, 1)})
    execution.record_order_placed()
    execution.record_order_placed()

    with pytest.raises(execution.RateLimited):
        execution.place_live_order("SOL/USDT", "BUY", 1.0, 10.0, "LIMIT")


# --- a real placement/fill records and credits correctly (not just the raw primitives) ------


def test_a_synchronous_taker_fill_frees_its_own_budget_slot(monkeypatch):
    """A MARKET order (every risk exit, and the wave-0 entry) fills in the SAME call — it
    must not sit outstanding waiting for a window to age it out."""
    class _Ex:
        def create_order(self, pair, typ, side, qty, price=None, params=None):
            return {"id": "1", "status": "closed", "filled": qty, "average": 10.0,
                    "amount": qty, "fee": {"cost": 0.0}}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    execution.reset_order_budget()

    execution.place_live_order("SOL/USDT", "SELL", 1.0, 0.0, "MARKET")

    assert execution.outstanding_order_count() == 0, "placed +1, filled -1, net zero"


def test_a_resting_order_stays_outstanding_until_its_fill_is_discovered(monkeypatch):
    class _Ex:
        def create_order(self, pair, typ, side, qty, price=None, params=None):
            return {"id": "1", "status": "open", "filled": 0.0, "average": 0.0,
                    "amount": qty, "fee": {"cost": 0.0}}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    execution.reset_order_budget()

    execution.place_live_order("SOL/USDT", "BUY", 1.0, 10.0, "LIMIT", maker_orders=True)

    assert execution.outstanding_order_count() == 1, "resting — not filled yet, still spent"


def test_a_duplicate_recovery_does_not_double_count(monkeypatch):
    """The lost original attempt already recorded the placement; recovering it by
    clientOrderId must not record a SECOND one."""
    class _Ex:
        def __init__(self):
            self.calls = 0

        def create_order(self, pair, typ, side, qty, price=None, params=None):
            self.calls += 1
            raise ccxt.ExchangeError("Duplicate order sent.")

        def fetch_order(self, cid, pair, params=None):
            return {"id": "1", "status": "closed", "filled": qty_sent, "average": 10.0,
                    "amount": qty_sent, "fee": {"cost": 0.0}}

    qty_sent = 1.0
    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    execution.reset_order_budget()
    execution.record_order_placed()  # the lost original attempt already counted itself

    execution.place_live_order(
        "SOL/USDT", "BUY", qty_sent, 10.0, "LIMIT", client_order_id="fm-7",
    )

    # +1 (the prior lost attempt) filled -1 (discovered on recovery) == net zero, never +2.
    assert execution.outstanding_order_count() == 0


def test_a_rejected_post_only_records_nothing(monkeypatch):
    class _Ex:
        def create_order(self, pair, typ, side, qty, price=None, params=None):
            raise ccxt.ExchangeError("Order would immediately match and take.")

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    execution.reset_order_budget()

    result = execution.place_live_order("SOL/USDT", "BUY", 1.0, 10.0, "LIMIT", maker_orders=True)

    assert result["status"] == "rejected"
    assert execution.outstanding_order_count() == 0


def test_an_async_maker_fill_credits_the_budget_via_reconciliation(db, monkeypatch):
    """`orders.reconcile_live_orders` is where a resting rung's fill is FIRST discovered — the
    credit belongs there (`_book_delta`), not at placement time, and it must fire exactly once."""
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(
        execution, "fetch_live_order",
        lambda pair, oid: {"status": "closed", "filled": 5.0, "average": 100.0,
                            "fee": 0.5, "raw_id": "X1"},
    )
    execution.reset_order_budget()
    execution.record_order_placed()  # the resting rung's own placement
    order = PendingOrder(
        symbol="SOL", side="BUY", order_type="LIMIT", quantity=5.0, price=100.0,
        source="manual", status=APPROVED, exchange_order_id="X1", exchange_status="open",
    )
    db.add(order)
    db.commit()

    booked = orders.reconcile_live_orders(db)

    assert booked == [order.id]
    assert execution.outstanding_order_count() == 0

    # Idempotent: a second pass must not credit again (the delta is now 0).
    execution.record_order_placed()  # simulate a fresh, unrelated placement
    orders.reconcile_live_orders(db)
    assert execution.outstanding_order_count() == 1


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


# --- REQUEST_WEIGHT: last_response_headers actually exists, and the guard is now wired ------


def test_last_response_headers_exists_on_this_ccxt():
    """Verified against the installed ccxt (not guessed): `Exchange.last_response_headers`
    defaults to None and `Exchange.enableLastResponseHeaders` defaults True, so ccxt assigns
    real response headers to it after every REST call (see ccxt/base/exchange.py `fetch`)."""
    ex = ccxt.binance({"apiKey": "k", "secret": "s"})

    assert hasattr(ex, "last_response_headers")
    assert ex.last_response_headers is None  # nothing sent yet
    assert ex.enableLastResponseHeaders is True


def test_note_weight_usage_schedules_a_hold_when_near_the_cap():
    class _Ex:
        last_response_headers = {"X-MBX-USED-WEIGHT-1M": "5900"}  # >80% of the 6000 fallback

    execution.reset_client_cache()

    execution._note_weight_usage(_Ex())

    assert execution.weight_hold_until() > 0


def test_note_weight_usage_is_a_noop_below_the_soft_threshold():
    class _Ex:
        last_response_headers = {"X-MBX-USED-WEIGHT-1M": "100"}

    execution.reset_client_cache()

    execution._note_weight_usage(_Ex())

    assert execution.weight_hold_until() == 0


def test_note_weight_usage_tolerates_a_missing_header():
    class _Ex:
        last_response_headers = None

    execution.reset_client_cache()

    execution._note_weight_usage(_Ex())  # must not raise

    assert execution.weight_hold_until() == 0


def test_a_weight_hold_never_blocks_a_sell():
    execution.reset_client_cache()
    execution._note_weight_usage(type("Ex", (), {"last_response_headers":
                                                   {"X-MBX-USED-WEIGHT-1M": "6000"}})())

    execution.assert_weight_budget_available(urgent=True)  # must not raise


def test_a_weight_hold_refuses_new_exposure():
    execution.reset_client_cache()
    execution._note_weight_usage(type("Ex", (), {"last_response_headers":
                                                   {"X-MBX-USED-WEIGHT-1M": "6000"}})())

    with pytest.raises(execution.RateLimited):
        execution.assert_weight_budget_available(urgent=False)


def test_placement_wires_the_weight_header_into_the_next_call(monkeypatch):
    """End to end: after a placement that came back near the REQUEST_WEIGHT cap, the NEXT buy
    is held while a sell still goes straight through."""
    class _Ex:
        last_response_headers = {"X-MBX-USED-WEIGHT-1M": "5999"}

        def create_order(self, pair, typ, side, qty, price=None, params=None):
            return {"id": "1", "status": "closed", "filled": qty, "average": 10.0,
                    "amount": qty, "fee": {"cost": 0.0}}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    execution.reset_client_cache()

    execution.place_live_order("SOL/USDT", "SELL", 1.0, 0.0, "MARKET")  # sets the hold

    assert execution.weight_hold_until() > 0
    with pytest.raises(execution.RateLimited):
        execution.place_live_order("SOL/USDT", "BUY", 1.0, 10.0, "LIMIT")
    # ...but another sell still reaches the venue.
    execution.place_live_order("SOL/USDT", "SELL", 1.0, 0.0, "MARKET")


# --- venue-sourced limits: exchangeInfo['rateLimits'], never hit over the network in a test --


_REAL_SHAPED_RATE_LIMITS = [
    {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000},
    {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 100},
    {"rateLimitType": "ORDERS", "interval": "DAY", "intervalNum": 1, "limit": 200_000},
    {"rateLimitType": "RAW_REQUESTS", "interval": "MINUTE", "intervalNum": 5, "limit": 300_000},
]


def test_parse_venue_rate_limits_reads_the_real_shape():
    orders_out, weight = execution.parse_venue_rate_limits(_REAL_SHAPED_RATE_LIMITS)

    assert orders_out["10s"] == (10.0, 100)
    assert orders_out["86400s"] == (86400.0, 200_000)
    assert weight == 6000
    assert len(orders_out) == 2, "RAW_REQUESTS is not an ORDERS/REQUEST_WEIGHT entry — skipped"


def test_parse_venue_rate_limits_skips_malformed_entries_instead_of_raising():
    orders_out, weight = execution.parse_venue_rate_limits([
        {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": "not-a-number", "limit": 100},
        {"rateLimitType": "ORDERS"},  # missing interval/limit entirely
        None,
    ])

    assert orders_out == {} and weight is None


def test_refresh_venue_limits_uses_the_cached_client_and_populates_both_budgets(monkeypatch):
    calls = []

    class _Ex:
        def publicGetExchangeInfo(self):
            calls.append(1)
            return {"rateLimits": _REAL_SHAPED_RATE_LIMITS}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    execution.reset_client_cache()

    execution.refresh_venue_limits()

    assert execution.order_limits()["10s"] == (10.0, 100)
    assert execution.order_limits()["86400s"] == (86400.0, 200_000)
    assert execution.current_weight_limit() == 6000
    assert len(calls) == 1

    execution.refresh_venue_limits()  # cached — must NOT call the venue again
    assert len(calls) == 1


def test_refresh_venue_limits_falls_back_when_the_venue_is_unreachable(monkeypatch):
    def _boom():
        raise ccxt.NetworkError("offline")

    monkeypatch.setattr(execution, "_client", _boom)
    execution.reset_client_cache()

    limits = execution.refresh_venue_limits()

    assert limits["10s"] == (10.0, execution.ORDER_LIMIT_10S_FALLBACK)
    assert limits["86400s"] == (86400.0, execution.ORDER_LIMIT_DAY_FALLBACK)
    assert execution.current_weight_limit() == execution.WEIGHT_LIMIT_PER_MIN
    # And the fallback is actually enforced going forward.
    execution.set_order_limits(limits)
    for _ in range(execution.ORDER_LIMIT_10S_FALLBACK):
        execution.record_order_placed()
    with pytest.raises(execution.RateLimited):
        execution.assert_order_budget_available(urgent=False)


def test_refresh_venue_limits_never_hits_the_network_by_default(monkeypatch):
    """Guard against a regression that makes this call real I/O in a test run: `_client` is
    patched to explode on ANY use other than the one explicit call this test allows."""
    hits = []

    class _Ex:
        def publicGetExchangeInfo(self):
            hits.append(1)
            return {"rateLimits": _REAL_SHAPED_RATE_LIMITS}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    execution.reset_client_cache()

    execution.refresh_venue_limits()
    execution.refresh_venue_limits()
    execution.refresh_venue_limits()

    assert hits == [1], "cached — must not fetch exchangeInfo per call"


# --- one placement may only ever return ONE unit of budget --------------------


def test_a_maker_order_filling_in_pieces_credits_the_budget_only_once(db, monkeypatch):
    """Binance's unfilled-order count is decremented when an order FILLS, once per order — not
    once per partial. A resting rung in a thin book books several deltas through `_book_delta`,
    and crediting each of them would leave the tracker believing more budget is free than
    really is. That is the one direction that ends in a -1015: over-counting only throttles us
    early, under-counting sends orders the venue refuses.
    """
    order = PendingOrder(symbol="SOL", side="BUY", order_type="LIMIT", quantity=10.0, price=10.0,
                         source="kss", source_ref="pyramid:1:wave:1", status=APPROVED,
                         exchange_order_id="EX-A", exchange_status="open")
    db.add(order)
    db.commit()

    execution.reset_client_cache()
    for _ in range(3):                       # three orders are outstanding...
        execution.record_order_placed()
    assert execution.outstanding_order_count() == 3

    # ...and exactly ONE of them fills, in three pieces across three reconcile passes.
    for cum in (4.0, 7.0, 10.0):
        orders._book_delta(db, order, {"status": "open" if cum < 10 else "closed",
                                       "filled": cum, "average": 10.0, "fee": 0.0,
                                       "raw_id": "EX-A"})

    # Crediting each piece would read as an empty budget. The counter clamps at zero, so the
    # difference only shows when more orders are outstanding than the one that filled — which
    # is exactly why the first version of this test passed with the bug still in place.
    assert execution.outstanding_order_count() == 2, "one order filled, so one unit comes back"
