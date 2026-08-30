"""We must never trade against ourselves, and the venue must never cancel our exit.

Binance's terms forbid self-trading and classify it as market manipulation; their Market
Surveillance team monitors for it and the penalty lands on the ACCOUNT, not the IP. This
strategy walks straight into it: under the 1.5 resting model an ACTIVE session leaves DCA
rung BUYs resting BELOW the market, and when the hard stop fires we send a MARKET SELL —
which sweeps down the book through exactly those rungs. Every stop-loss is a self-match
waiting to print.

Production Binance refuses `selfTradePreventionMode=NONE` outright (queried 2026-08-30:
real allows EXPIRE_TAKER/EXPIRE_MAKER/EXPIRE_BOTH/DECREMENT/TRANSFER, testnet also allows
NONE), so the match itself cannot be opted out of. What is NOT settled by the venue is WHICH
side dies, and that is an account-level default we do not control:

* EXPIRE_MAKER — our resting rung is cancelled, our stop-loss executes. The exit survives.
* EXPIRE_TAKER / EXPIRE_BOTH — the venue cancels our STOP-LOSS. Silently, mid-crash, with
  the position still open.

So the mode is not left to the account default. Every live order names EXPIRE_MAKER, and the
one unforgivable bug in this project — a gated exit — stays impossible even if someone
changes the account setting in the Binance UI.
"""

from __future__ import annotations

import pytest

from app import execution

# Binance resolves a self-match using the TAKER's mode, so the safe mode depends on which side
# we are taking with. Selling, we are the taker and the maker is a DCA rung: expire the maker.
# Buying, we are the taker and the maker may be our own resting take-profit: expire the taker.
# Either way the SELL survives, which is the only rule that matters.
SAFE_SELL_MODE = "EXPIRE_MAKER"
SAFE_BUY_MODE = "EXPIRE_TAKER"
NEVER = {"NONE", "EXPIRE_BOTH"}


class _Recorder:
    """Captures the params dict actually handed to ccxt."""

    def __init__(self):
        self.calls: list[dict] = []

    def create_order(self, pair, typ, side, qty, price=None, params=None):
        self.calls.append({"type": typ, "side": side, "qty": qty, "price": price,
                           "params": dict(params or {})})
        return {"id": "1", "status": "closed", "filled": qty, "average": price or 10.0,
                "amount": qty, "fee": {"cost": 0.0}}

    @property
    def params(self) -> dict:
        return self.calls[-1]["params"]


@pytest.fixture
def venue(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(execution, "_client", lambda: rec)
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})
    return rec


def test_a_market_exit_expires_the_rung_not_the_stop(venue):
    """The reachable case: a stop-loss sweeping down through our own resting rungs. We are the
    taker, so our mode decides — EXPIRE_MAKER kills the rung and the stop-loss executes."""
    execution.place_live_order("SOL/USDT", "SELL", 1.0, 0.0, "MARKET")

    assert venue.params.get("selfTradePreventionMode") == SAFE_SELL_MODE


def test_a_market_entry_expires_itself_rather_than_our_take_profit(venue):
    """The mirror case, and the one that makes a single fixed mode wrong.

    A `pyramid_up` add (and a wave-0 entry when two sessions share a symbol) is a taker BUY,
    while `sync_resting_tp` keeps a LIMIT SELL resting for every ACTIVE session. If that BUY
    sweeps up to our own take-profit, EXPIRE_MAKER would have Binance cancel THE EXIT — and the
    app would not notice: the row keeps `exchange_status='open'`, so the dead-link reaper skips
    it and only `reconcile_live_orders` (slow cycle, up to `scan_interval_min`) ever stamps it
    expired. A cancelled entry is retried next cycle; a cancelled exit is the unforgivable bug.
    """
    execution.place_live_order("SOL/USDT", "BUY", 1.0, 0.0, "MARKET")

    assert venue.params.get("selfTradePreventionMode") == SAFE_BUY_MODE


def test_a_resting_rung_names_the_self_trade_mode(venue):
    execution.place_live_order("SOL/USDT", "BUY", 1.0, 10.0, "LIMIT", maker_orders=True)

    assert venue.params.get("selfTradePreventionMode") == SAFE_BUY_MODE
    assert venue.params.get("postOnly") is True, "still post-only — this is additive"


def test_a_resting_exit_names_the_self_trade_mode(venue):
    execution.place_live_order("SOL/USDT", "SELL", 1.0, 12.0, "LIMIT", maker_orders=True)

    assert venue.params.get("selfTradePreventionMode") == SAFE_SELL_MODE


def test_no_side_or_kind_can_ever_cancel_our_exit():
    """Whatever we send, the SELL must be the order that survives a self-match."""
    for kind in ("MARKET", "LIMIT"):
        for maker in (True, False):
            _, sell = execution.order_placement(kind, maker_orders=maker, side="SELL")
            _, buy = execution.order_placement(kind, maker_orders=maker, side="BUY")
            assert sell["selfTradePreventionMode"] == SAFE_SELL_MODE
            assert buy["selfTradePreventionMode"] == SAFE_BUY_MODE
            assert sell["selfTradePreventionMode"] not in NEVER
            assert buy["selfTradePreventionMode"] not in NEVER


def test_the_mode_is_never_none():
    """NONE is what permits the self-trade to actually print. Production Binance rejects it,
    but we must not be the ones asking — the request itself is the compliance record."""
    for side in ("BUY", "SELL"):
        for kind in ("MARKET", "LIMIT"):
            _, params = execution.order_placement(kind, maker_orders=True, side=side)
            assert params["selfTradePreventionMode"] != "NONE"


def test_an_idempotency_key_still_travels_with_it(venue):
    """Regression: the STP param must not displace clientOrderId (1.10 duplicate recovery)."""
    execution.place_live_order("SOL/USDT", "BUY", 1.0, 10.0, "LIMIT", client_order_id="po-7")

    assert venue.params.get("clientOrderId") == "po-7"
    assert venue.params.get("selfTradePreventionMode") == SAFE_BUY_MODE


def test_a_non_binance_venue_is_not_sent_a_binance_only_param(venue, monkeypatch):
    """`selfTradePreventionMode` is Binance's. `live_exchange` is a free-form string any .env
    can repoint, and an unknown param would be rejected on EVERY order — stop-losses too."""
    monkeypatch.setattr(execution.settings, "live_exchange", "okx")

    execution.place_live_order("SOL/USDT", "SELL", 1.0, 0.0, "MARKET")

    assert "selfTradePreventionMode" not in venue.params
