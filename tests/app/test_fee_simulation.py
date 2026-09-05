"""Make the venue's commission visible on testnet, so the loss path is rehearsed WITH a fee.

Binance testnet charges zero commission. That is precisely why the 2026-08-31 bug — booking a
spot BUY's gross `filled` while the wallet received less, so every exit is rejected -2010 —
survived 24 live fills without a symptom. The fix shipped, but it has still never executed
against a venue that actually charges anything: `fee_base_qty` returns 0.0 on every testnet
fill, so the subtraction is a no-op and the whole downstream (exit quantity, step rounding,
minNotional, K-2) is exercised on numbers a real account will never produce.

So we synthesise it. On TESTNET only, when the venue reports no base-asset commission, book the
BUY as if it had charged `simulated_fee_pct`. The error then leans the SAFE way: the book holds
LESS coin than the testnet wallet really has, so an exit can never come up short — while every
downstream calculation sees mainnet-shaped numbers.

On top of that, `fee_safety_margin_pct` (both environments) shaves a further sliver off the
booked quantity. The book is then deliberately a hair poorer than the wallet, so a fee tier we
mis-read, a rounding step, or a commission we failed to parse cannot leave an exit asking the
venue for coin that is not there. The cost is dust — 0.05% of a $40 position is two cents.

Neither term may ever be large enough to matter to the position itself: a misconfigured knob
must not be able to eat the fill, so the total is capped at 1% of it.
"""

from __future__ import annotations

import pytest

from app import execution
from app.config import settings


def _buy(filled: float = 100.0, fee_entries: list | None = None) -> dict:
    order = {"id": "1", "status": "closed", "side": "buy", "filled": filled,
             "amount": filled, "average": 10.0, "fee": {"cost": None}}
    if fee_entries:
        order["fees"] = fee_entries
    return order


def _knobs(monkeypatch, *, testnet: bool, sim: float = 0.1, margin: float = 0.05) -> None:
    monkeypatch.setattr(settings, "live_use_testnet", testnet)
    monkeypatch.setattr(settings, "simulated_fee_pct", sim)
    monkeypatch.setattr(settings, "fee_safety_margin_pct", margin)


def test_testnet_synthesises_the_commission_the_venue_never_charged(monkeypatch):
    """The whole point: a zero-fee testnet fill books like a mainnet one."""
    _knobs(monkeypatch, testnet=True)

    booked = execution.booked_fee_base(_buy(filled=100.0), base="SOL", filled=100.0, side="BUY")

    # 0.1% simulated commission + 0.05% safety margin, both on the gross fill.
    assert booked == pytest.approx(0.15)


def test_mainnet_never_synthesises_anything(monkeypatch):
    """A real account's zero-fee answer is the venue's word, not ours to overwrite — the only
    thing added there is the safety margin."""
    _knobs(monkeypatch, testnet=False)

    booked = execution.booked_fee_base(_buy(filled=100.0), base="SOL", filled=100.0, side="BUY")

    assert booked == pytest.approx(0.05)


def test_a_real_commission_is_never_doubled(monkeypatch):
    """Testnet is the simulator's ONLY trigger, and even there a venue that did report a
    commission wins — otherwise the book would be charged twice for one fill."""
    _knobs(monkeypatch, testnet=True)
    order = _buy(filled=100.0, fee_entries=[{"cost": 0.09, "currency": "SOL"}])

    booked = execution.booked_fee_base(order, base="SOL", filled=100.0, side="BUY")

    assert booked == pytest.approx(0.09 + 0.05)


def test_a_sell_is_never_charged_in_the_base_asset(monkeypatch):
    """A SELL's commission comes out of the PROCEEDS (quote), so its quantity stands whole —
    shaving it here would under-sell the position and strand the remainder."""
    _knobs(monkeypatch, testnet=True)
    order = _buy(filled=100.0)
    order["side"] = "sell"

    assert execution.booked_fee_base(order, base="SOL", filled=100.0, side="SELL") == 0.0


def test_nothing_is_booked_against_a_resting_order_that_has_not_filled(monkeypatch):
    _knobs(monkeypatch, testnet=True)

    assert execution.booked_fee_base(_buy(filled=0.0), base="SOL", filled=0.0, side="BUY") == 0.0


def test_a_misconfigured_knob_can_never_eat_the_fill(monkeypatch):
    """The safety margin is a sliver by design. Capped at 1% of the fill so a fat-fingered knob
    cannot quietly shrink a position — an exit sized off a gutted quantity would strand coin."""
    _knobs(monkeypatch, testnet=True, sim=1.0, margin=1.0)

    booked = execution.booked_fee_base(_buy(filled=100.0), base="SOL", filled=100.0, side="BUY")

    assert booked == pytest.approx(1.0), "total must clamp to 1% of the fill"


def test_the_margin_is_proportional_so_partial_fills_stay_consistent(monkeypatch):
    """`_book_delta` works on CUMULATIVE quantities: booking must stay proportional to the fill
    or a second reconcile would compute a nonsense delta."""
    _knobs(monkeypatch, testnet=True)

    half = execution.booked_fee_base(_buy(filled=50.0), base="SOL", filled=50.0, side="BUY")
    full = execution.booked_fee_base(_buy(filled=100.0), base="SOL", filled=100.0, side="BUY")

    assert full == pytest.approx(2 * half)


# --- the wiring: the three places a live fill is read must all use it -------------------


class _FakeEx:
    def __init__(self, order):
        self._order = order

    def create_order(self, pair, otype, side, qty, price=None, params=None):
        return self._order

    def fetch_order(self, order_id, pair=None, params=None):
        return self._order


def test_place_live_order_books_the_simulated_commission(monkeypatch):
    """The synchronous path (every MARKET risk exit and the wave-0 entry)."""
    _knobs(monkeypatch, testnet=True)
    order = {"status": "closed", "filled": 100.0, "amount": 100.0, "average": 10.0,
             "side": "buy", "fee": {"cost": None}, "id": "m1"}
    monkeypatch.setattr(execution, "_client", lambda: _FakeEx(order))

    res = execution.place_live_order("SOL/USDT", "BUY", 100.0, 0.0, "MARKET")

    assert res["fee_base"] == pytest.approx(0.15)


def test_fetch_live_order_books_the_simulated_commission(monkeypatch):
    """The resting path — where MOST fills are booked under the 1.5 model."""
    _knobs(monkeypatch, testnet=True)
    order = {"status": "closed", "filled": 100.0, "amount": 100.0, "average": 10.0,
             "side": "buy", "fee": {"cost": None}, "id": "r1"}
    monkeypatch.setattr(execution, "_client", lambda: _FakeEx(order))

    res = execution.fetch_live_order("SOL/USDT", "r1")

    assert res["fee_base"] == pytest.approx(0.15)
