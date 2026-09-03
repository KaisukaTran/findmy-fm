"""The rate-limit hold must not block an exit, and must not fire on a network blip.

Both faults are mine, from the previous commit, caught by the cross-check.

`classify_rate_error` matched the bare substrings "418"/"429" against the whole exception text.
ccxt puts the request URL in that text, and a Binance signed URL carries `orderId`, a 13-digit
timestamp and a 64-hex signature — so roughly 3% of ordinary network errors contain "418"
somewhere and were read as an IP ban. That set a 300-second hold.

And the hold was unconditional on side, so it blocked SELLs. This project has exactly one
unforgivable bug: slowing an exit. A throttle signal must never do it — a stop-loss is one
weight unit, and refusing to send it because a market-data call was throttled trades a real
position for an imaginary saving.
"""

from __future__ import annotations

import pytest
from ccxt.base.errors import DDoSProtection, RequestTimeout

from app import execution


@pytest.fixture(autouse=True)
def _clean():
    execution.reset_client_cache()
    yield
    execution.reset_client_cache()


_SIGNED_URL_TIMEOUT = (
    "binance GET https://api.binance.com/api/v3/order?symbol=SOLUSDT"
    "&orderId=4180192837&timestamp=1756500000429&signature=deadbeef read timeout"
)


def test_a_network_timeout_is_not_a_ban_just_because_the_url_contains_418():
    action, _ = execution.classify_rate_error(RequestTimeout(_SIGNED_URL_TIMEOUT))

    assert action == "raise", "an order id is not an HTTP status"


def test_a_real_rate_limit_is_still_detected():
    action, wait = execution.classify_rate_error(DDoSProtection("binance 429 Too Many Requests"))

    assert action == "retry" and wait


def test_a_real_ban_is_still_detected():
    exc = DDoSProtection("binance 418 I'm a teapot")
    exc.http_status_code = 418

    assert execution.classify_rate_error(exc)[0] == "halt"


def test_a_small_retry_after_never_under_pauses_a_ban():
    """Fix round A / item 5(b): `note_rate_error`'s halt branch used to pause for
    `float(retry_after or 300.0)` — a SMALL/garbage Retry-After (5s) would under-pause a 418 IP
    ban. Floor it at 300s regardless of what the venue sent."""
    import time as _time

    action = execution.note_rate_error(_ban(), retry_after=5.0)

    assert action == "halt"
    assert execution.rate_limited_until() - _time.monotonic() >= 299.0


def test_a_hold_never_blocks_an_exit():
    execution.note_rate_error(_ban())

    execution.assert_not_rate_limited(urgent=True)  # must not raise


def test_a_hold_blocks_new_exposure():
    execution.note_rate_error(_ban())

    with pytest.raises(execution.RateLimited):
        execution.assert_not_rate_limited(urgent=False)


def test_placing_a_sell_survives_a_hold(monkeypatch):
    """End to end: a stop-loss must reach the venue during a 418 hold."""
    sent = []

    class _Ex:
        def create_order(self, pair, typ, side, qty, price=None, params=None):
            sent.append(side)
            return {"id": "1", "status": "closed", "filled": qty, "average": 10.0,
                    "amount": qty, "fee": {"cost": 0.0}}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    execution.note_rate_error(_ban())

    execution.place_live_order("SOL/USDT", "SELL", 1.0, 0.0, "MARKET")

    assert sent == ["sell"]


def test_placing_a_buy_is_refused_during_a_hold(monkeypatch):
    sent = []

    class _Ex:
        def create_order(self, *a, **k):
            sent.append(a)
            return {}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    execution.note_rate_error(_ban())

    with pytest.raises(execution.RateLimited):
        execution.place_live_order("SOL/USDT", "BUY", 1.0, 10.0, "LIMIT")
    assert sent == [], "new exposure must not reach a venue that is rate-limiting us"


def _ban() -> Exception:
    exc = DDoSProtection("binance 418 banned")
    exc.http_status_code = 418
    return exc
