"""A MARKET order must satisfy the venue's filters too — and one bad order must not stop the rest.

Both found by the soak, minutes apart. Wave 0 now goes out as MARKET with a quantity derived
from a USD size (first_wave_usd / price), so it lands on ragged numbers like 0.30749... Only
post-only LIMITs were being rounded to the symbol's filters, so Binance answered -1013
"Filter failure: NOTIONAL" — and because auto_fill_due_orders only caught ValueError while ccxt
raises InvalidOrder, that one order killed the entire scheduler cycle.

The exit invariant still rules: a SELL that reduces risk is sent even when rounding would
refuse it. Never blocking an exit outranks tidy compliance.
"""

from __future__ import annotations

import pytest
from ccxt.base.errors import InvalidOrder

from app import execution, orders
from app.config import settings
from app.models import PENDING, PendingOrder


class _Ex:
    """Records what actually reached create_order."""

    def __init__(self):
        self.sent: list[dict] = []

    def create_order(self, pair, typ, side, qty, price=None, params=None):
        self.sent.append({"pair": pair, "type": typ, "side": side, "qty": qty, "price": price})
        return {"id": "1", "status": "closed", "average": price or 10.0, "filled": qty,
                "price": price or 10.0, "fee": {"cost": 0.0}}


_FILTERS = {"tickSize": 0.01, "stepSize": 0.001, "minQty": 0.001,
            "minNotional": 5.0, "percentUp": 5.0, "percentDown": 0.2}


def _patch(monkeypatch, ex):
    monkeypatch.setattr(execution, "_client", lambda: ex)
    monkeypatch.setattr(execution, "_market_filters", lambda e, pair: dict(_FILTERS))
    monkeypatch.setattr(settings, "live_exchange", "binance")


def test_a_market_buy_is_rounded_to_the_lot_step(monkeypatch):
    ex = _Ex()
    _patch(monkeypatch, ex)

    execution.place_live_order("LTC/USDT", "BUY", 0.3074915, 48.78, "MARKET")

    sent = ex.sent[0]
    assert sent["type"] == "market"
    # 0.3074915 -> 0.307 at a 0.001 step; the raw value is what the venue rejected.
    assert sent["qty"] == pytest.approx(0.307)


def test_a_market_buy_under_min_notional_is_refused_before_it_is_sent(monkeypatch):
    ex = _Ex()
    _patch(monkeypatch, ex)

    with pytest.raises(ValueError):
        execution.place_live_order("LTC/USDT", "BUY", 0.02, 48.78, "MARKET")

    assert ex.sent == [], "a doomed BUY should not reach the exchange at all"


def test_an_exit_sell_is_sent_even_when_rounding_would_refuse_it(monkeypatch):
    """Exits are never gated — a dust position must still be sellable."""
    ex = _Ex()
    _patch(monkeypatch, ex)

    execution.place_live_order("LTC/USDT", "SELL", 0.02, 48.78, "MARKET")

    assert len(ex.sent) == 1
    assert ex.sent[0]["side"] == "sell"


def test_one_rejected_order_does_not_stop_the_others(db, monkeypatch):
    """auto_fill_due_orders caught only ValueError; ccxt raises InvalidOrder, which escaped
    and killed the cycle."""
    def _kss(symbol, price):
        o = PendingOrder(symbol=symbol, side="BUY", order_type="MARKET", quantity=1.0,
                         price=price, source="kss", source_ref=f"pyramid:1:wave:0",
                         status=PENDING)
        db.add(o)
        return o

    bad, good = _kss("LTC", 48.78), _kss("DOT", 0.83)
    db.commit()
    monkeypatch.setattr("app.orders.get_current_prices",
                        lambda syms: {"LTC": 48.78, "DOT": 0.83})
    monkeypatch.setattr(settings, "auto_trade", True)

    calls: list[int] = []

    def _approve(session, order_id, reviewer=None):
        calls.append(order_id)
        if order_id == bad.id:
            raise InvalidOrder('binance {"code":-1013,"msg":"Filter failure: NOTIONAL"}')
        return None

    monkeypatch.setattr(orders, "approve_order", _approve)

    approved = orders.auto_fill_due_orders(db)

    assert bad.id in calls and good.id in calls, "the loop must continue past the failure"
    assert approved == [good.id]


def test_filters_are_loaded_even_on_a_cold_ccxt_client(monkeypatch):
    """ccxt's .market() raises "markets not loaded" until something has loaded them, and the
    caller then placed UNROUNDED — which is how a -1013 reached the venue despite the rounding
    above. Third instance of this same trap in the codebase (get_exchange_info, the testnet
    harness, here), so it is handled at the source now."""
    calls = {"loaded": 0}

    class _Cold:
        def load_markets(self):
            calls["loaded"] += 1

        def market(self, pair):
            if not calls["loaded"]:
                raise Exception("binance markets not loaded")
            return {"limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}},
                    "precision": {"price": 2, "amount": 3},
                    "info": {"filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.00100000", "minQty": "0.00100000"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
                    ]}}

    out = execution._market_filters(_Cold(), "LTC/USDT")

    assert calls["loaded"] == 1
    assert out.get("stepSize") == pytest.approx(0.001)
