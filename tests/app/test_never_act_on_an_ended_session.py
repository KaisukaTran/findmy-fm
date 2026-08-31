"""A rung whose session has ended must never be placed or filled, however it got queued.

My `sweep_orphan_waves` guard skipped rows carrying a terminal `exchange_status`, to avoid
stranding a fill only the dead-link reaper could book. But that is EXACTLY the set the reaper
then releases, later in the same cycle — and once the link is NULL the row is an ordinary
queued rung again, and nothing re-checks whose session it belongs to. `auto_fill_due_orders`
MARKET-buys wave 0; the resting `due` loop re-rests waves 1+.

It already happened on the live book: `resting_link_released` on order 4 (ARB) at 06:16:20,
then 172 ARB booked to `pyramid:4:wave:0` at 06:16:23 — into a session STOPPED for 17 hours.
I previously attributed that fill to a pre-existing resting order. It was the app re-buying,
three seconds after its own reaper.

The new self-trade-prevention mode feeds this: a stop-loss sweeping our own rung EXPIRES it,
ccxt maps that to `'expired'`, which is terminal — so the sweep skips it, the reaper frees it,
and the app buys back into the fall it just sold to escape.

So the check belongs at the point of ACTION, not only in the sweep: whatever put a rung in the
queue, refuse to send it if its session is no longer going. Belt and braces for the one pattern
that has produced most of this project's realised loss.
"""

from __future__ import annotations

import pytest

from app import execution, models, orders
from app.config import settings
from app.models import PENDING, PendingOrder, Position


def _session(db, status):
    row = models.KssSession(symbol="ARB", entry_price=0.0865, distance_pct=2.0, max_waves=3,
                            isolated_fund=140.0, tp_pct=3.0, timeout_x_min=60, gap_y_min=5,
                            status=status, current_wave=1, avg_price=0.0865,
                            total_filled_qty=0.0, total_cost=0.0, sl_pct=8.0)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _rung(db, session_id, wave=0):
    o = PendingOrder(symbol="ARB", side="BUY", order_type="LIMIT", quantity=172.0, price=0.0865,
                     source="kss", source_ref=f"pyramid:{session_id}:wave:{wave}", status=PENDING)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


@pytest.fixture
def venue(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(settings, "live_max_order_notional", 10_000.0)
    monkeypatch.setattr(settings, "auto_trade", True)
    monkeypatch.setattr("app.data.providers.live_provider",
                        lambda: type("P", (), {"pair": staticmethod(lambda s: f"{s}/USDT")})())
    monkeypatch.setattr(execution, "place_live_order",
                        lambda *a, **k: sent.append({"a": a}) or
                        {"raw_id": "EX-9", "status": "closed", "price": 0.0865,
                         "quantity": 172.0, "fee": 0.0, "fee_base": 0.0})
    return sent


@pytest.mark.parametrize("status", [models.SESSION_STOPPED, models.SESSION_COMPLETED])
def test_a_wave_0_rung_of_an_ended_session_is_never_market_bought(db, venue, monkeypatch, status):
    """The live ARB shape, at the last line of defence."""
    s = _session(db, status)
    order = _rung(db, s.id, wave=0)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"ARB": 0.0800})

    orders.auto_fill_due_orders(db)

    db.refresh(order)
    assert venue == [], "a stopped session must not buy"
    assert db.query(Position).filter(Position.symbol == "ARB").one_or_none() is None


@pytest.mark.parametrize("status", [models.SESSION_ACTIVE, models.SESSION_PENDING,
                                    models.SESSION_TP_TRIGGERED])
def test_a_rung_of_a_session_still_going_is_allowed(db, status):
    """Guard the fix does not simply stop the strategy working. `tp_triggered` is transient —
    K-2 can put the session straight back to ACTIVE — so it must pass too."""
    s = _session(db, status)
    order = _rung(db, s.id, wave=0)

    assert orders.session_still_going(db, order.source_ref) is True


def test_a_row_with_no_session_at_all_is_left_to_the_sweep(db):
    """A MISSING session is `sweep_orphan_waves`'s job; refusing here would block ordinary
    flows whose fixtures never created one."""
    assert orders.session_still_going(db, "pyramid:4242:wave:1") is True


def test_a_non_pyramid_row_is_none_of_this_check_s_business(db):
    assert orders.session_still_going(db, "manual:1") is True
    assert orders.session_still_going(db, None) is True
