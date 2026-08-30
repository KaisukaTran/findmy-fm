"""A live fill must record the commission it actually paid.

Binance spot returns no top-level `fee` on the order endpoints; the real commissions live in
`fees` / the `trades` array. ccxt still constructs `order['fee'] = {'cost': None, ...}`, and
because that dict is not None it skips promoting the per-trade commissions. Reading only
`order['fee']['cost']` therefore produced **0.0 on every live fill** — confirmed against a real
Binance-testnet fill of 221.9 units recorded with sum(fee) = 0.

Every downstream number is then wrong in the same direction: avg_entry_price and total_cost
understate, realized P&L overstates by roughly the round-trip commission, the cash floor
over-permits, and K-2 — which is supposed to keep an exit above true cost + 2x fee — degrades
toward break-even. Invisible on testnet, where commissions are zero.
"""

from __future__ import annotations

import pytest

from app import execution


def test_the_fee_comes_from_the_fees_list_when_the_top_level_is_empty():
    order = {
        "id": "1", "status": "closed", "filled": 2.0, "average": 100.0, "amount": 2.0,
        "fee": {"cost": None, "currency": "USDT", "rate": None},
        "fees": [{"cost": 0.2, "currency": "USDT"}],
    }

    assert execution.fee_cost(order, quote="USDT") == pytest.approx(0.2)


def test_per_trade_commissions_are_summed():
    """A market order fills across several trades; each carries its own commission."""
    order = {
        "id": "1", "status": "closed", "filled": 3.0, "average": 100.0,
        "fee": {"cost": None},
        "trades": [
            {"fee": {"cost": 0.05, "currency": "USDT"}},
            {"fee": {"cost": 0.07, "currency": "USDT"}},
        ],
    }

    assert execution.fee_cost(order, quote="USDT") == pytest.approx(0.12)


def test_a_top_level_fee_is_still_honoured():
    order = {"id": "1", "fee": {"cost": 0.3, "currency": "USDT"}}

    assert execution.fee_cost(order, quote="USDT") == pytest.approx(0.3)


def test_a_base_asset_commission_is_not_counted_as_quote():
    """On a spot BUY Binance takes the commission in the BASE asset. Adding that number to a
    USD cost basis is nonsense — 0.0003 BTC is not $0.0003."""
    order = {
        "id": "1", "filled": 0.3, "average": 80.0, "fee": {"cost": None},
        "fees": [{"cost": 0.0003, "currency": "LTC"}],
    }

    assert execution.fee_cost(order, quote="USDT") == 0.0


def test_the_base_commission_is_reported_separately():
    """It still matters: the wallet received LESS base than `filled` says, which is what makes
    a later full-size exit fail with -2010."""
    order = {
        "id": "1", "filled": 0.3, "average": 80.0, "fee": {"cost": None},
        "fees": [{"cost": 0.0003, "currency": "LTC"}],
    }

    assert execution.fee_base_qty(order, base="LTC") == pytest.approx(0.0003)


def test_no_fee_information_at_all_is_zero_not_a_crash():
    assert execution.fee_cost({"id": "1"}, quote="USDT") == 0.0
    assert execution.fee_base_qty({"id": "1"}, base="LTC") == 0.0


def test_place_live_order_reports_the_real_fee(monkeypatch):
    class _Ex:
        def create_order(self, pair, typ, side, qty, price=None, params=None):
            return {"id": "T1", "status": "closed", "filled": 2.0, "average": 100.0,
                    "amount": 2.0, "fee": {"cost": None, "currency": "USDT"},
                    "fees": [{"cost": 0.2, "currency": "USDT"}]}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    monkeypatch.setattr(execution, "_market_filters", lambda e, p: {})

    out = execution.place_live_order("LTC/USDT", "BUY", 2.0, 100.0, "MARKET")

    assert out["fee"] == pytest.approx(0.2), "a booked fill must carry its commission"
