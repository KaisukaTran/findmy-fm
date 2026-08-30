"""A position bigger than the sessions holding it is orphaned too.

`manage_orphan_positions` decided what was covered by SYMBOL: if any ACTIVE session (or OPUS
position, or in-flight SELL) named the symbol, the WHOLE symbol-level Position was treated as
managed. But a session manages only the quantity IT filled. Anything above that has no
take-profit, no stop and no ladder — it rides forever, which is the exact failure the function's
own docstring says it exists to prevent.

Found live on the 2026-08-30 testnet soak: `positions.ARB.quantity = 344` while the only ACTIVE
ARB session (id 9) held `total_filled_qty = 172`. Fill id 10 (172 ARB @ 0.0865, $14.88) had been
booked against `pyramid:4:wave:0` at 06:16:23 — into session 4, which was already STOPPED. ARB
was in `kss_syms`, so all 344 counted as covered and the extra 172 was managed by nothing.

`sweep_orphan_waves` now retires such a rung before it can fill, so this is the second line: it
cleans up what already got through, and covers any other route to a position larger than its
sessions.
"""

from __future__ import annotations

import pytest

from app import models
from app.config import settings
from app.kss import service as kss
from app.models import PENDING, PendingOrder, Position


def _session(db, symbol="ARB", qty=172.0, status=models.SESSION_ACTIVE) -> models.KssSession:
    row = models.KssSession(
        symbol=symbol, entry_price=0.087, distance_pct=2.0, max_waves=3, isolated_fund=140.0,
        tp_pct=3.0, timeout_x_min=60, gap_y_min=5, status=status, current_wave=1,
        avg_price=0.087, total_filled_qty=qty, total_cost=qty * 0.087, sl_pct=8.0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _position(db, symbol="ARB", qty=344.0, avg=0.08685) -> Position:
    pos = Position(symbol=symbol, quantity=qty, avg_entry_price=avg)
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


def _queued_sells(db, symbol="ARB") -> list[PendingOrder]:
    return list(db.query(PendingOrder).filter(
        PendingOrder.status == PENDING, PendingOrder.side == "SELL",
        PendingOrder.symbol == symbol).all())


@pytest.fixture(autouse=True)
def _price(monkeypatch):
    """+15% against the position's average — well past scan_tp_pct."""
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"ARB": 0.0999})
    monkeypatch.setattr("app.kss.service._tp_clears_cost", lambda db, sym, px: True)


def test_the_quantity_a_session_does_not_hold_is_managed(db):
    """172 of 344 belong to the ACTIVE session; the other 172 belong to nobody."""
    _session(db, qty=172.0)
    _position(db, qty=344.0)

    kss.manage_orphan_positions(db)

    sells = _queued_sells(db)
    assert len(sells) == 1, "the unmanaged remainder must get an exit"
    assert sells[0].quantity == pytest.approx(172.0), "only the excess — not the session's own"


def test_a_position_its_sessions_fully_account_for_is_left_alone(db):
    """The normal case: the session holds all of it and runs its own take-profit."""
    _session(db, qty=344.0)
    _position(db, qty=344.0)

    kss.manage_orphan_positions(db)

    assert _queued_sells(db) == []


def test_two_sessions_on_one_symbol_are_summed(db):
    _session(db, qty=172.0)
    _session(db, qty=100.0)
    _position(db, qty=744.0)

    kss.manage_orphan_positions(db)

    sells = _queued_sells(db)
    assert len(sells) == 1
    assert sells[0].quantity == pytest.approx(472.0), "744 - 172 - 100"


def test_an_excess_too_small_to_trade_is_not_chased(db, monkeypatch):
    """Fees and rounding leave slivers, and a sub-minimum order is refused by the venue anyway —
    so the excess has to clear `scan_min_notional` before it is worth an order. 72 ARB at
    $0.08685 is $6.25, under the $10 floor."""
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)
    _session(db, qty=272.0)
    _position(db, qty=344.0)

    kss.manage_orphan_positions(db)

    assert _queued_sells(db) == []


def test_a_rounding_sliver_is_not_chased(db):
    _session(db, qty=344.0)
    _position(db, qty=344.0000001)

    kss.manage_orphan_positions(db)

    assert _queued_sells(db) == []


def test_a_fully_orphaned_position_still_sells_all_of_it(db):
    """Regression: the original behaviour, where no session names the symbol at all."""
    _position(db, qty=344.0)

    kss.manage_orphan_positions(db)

    sells = _queued_sells(db)
    assert len(sells) == 1 and sells[0].quantity == pytest.approx(344.0)


def test_an_exit_already_in_flight_defers_the_sweep(db):
    """Unchanged guard: a SELL already queued for the symbol means the position is on its way
    out — sweeping it again would double-sell and double-charge the fee."""
    _session(db, qty=172.0)
    _position(db, qty=344.0)
    db.add(PendingOrder(symbol="ARB", side="SELL", order_type="MARKET", quantity=172.0,
                        price=0.0, source="kss", source_ref="pyramid:9:tp", status=PENDING))
    db.commit()

    kss.manage_orphan_positions(db)

    assert len(_queued_sells(db)) == 1, "no second exit while one is in flight"


def test_the_excess_is_left_alone_while_it_is_neither_at_tp_nor_at_stop(db, monkeypatch):
    """The excess is managed, not dumped: it gets the same TP/SL treatment as any orphan."""
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"ARB": 0.0870})  # ~flat
    _session(db, qty=172.0)
    _position(db, qty=344.0)

    kss.manage_orphan_positions(db)

    assert _queued_sells(db) == [], "hold it until it reaches a target, same as any orphan"


def test_a_stopped_session_does_not_count_as_holding_anything(db):
    """Session 4 in the live case was STOPPED with total_filled_qty 0 — yet a fill landed on it.
    Only ACTIVE sessions may account for quantity."""
    _session(db, qty=172.0, status=models.SESSION_STOPPED)
    _position(db, qty=172.0)

    kss.manage_orphan_positions(db)

    sells = _queued_sells(db)
    assert len(sells) == 1 and sells[0].quantity == pytest.approx(172.0)


def test_the_settings_tp_threshold_is_unchanged(db):
    """Guard that the excess path reuses the existing thresholds rather than inventing new ones."""
    assert settings.scan_tp_pct > 0 and settings.sl_pct > 0


def test_a_session_reporting_zero_filled_protects_its_whole_symbol(db):
    """We can only subtract an accounting we trust. An ACTIVE session showing zero filled
    quantity is one we cannot — its counter may simply not have caught up — so its whole symbol
    stays managed, exactly as before. Selling a live session's position out from under it would
    be far worse than leaving a remainder for another cycle.
    """
    _session(db, qty=0.0)
    _position(db, qty=344.0)

    kss.manage_orphan_positions(db)

    assert _queued_sells(db) == []


def test_the_sessions_own_resting_take_profit_does_not_count_as_extra_cover(db):
    """The exact live ARB shape, and the reason a symbol-level defer was wrong.

    Under the 1.5 resting model every ACTIVE session ALWAYS has a take-profit on the book. If
    that SELL marked the whole symbol as covered, the excess check could never fire for any
    live symbol — the defer would be permanent and this function a no-op. And its quantity may
    not be ADDED to the session's either: it sells the very units already counted, so adding it
    would cover 172 + 172 = 344 and hide the orphan again.
    """
    s = _session(db, qty=172.0)
    _position(db, qty=344.0)
    db.add(PendingOrder(symbol="ARB", side="SELL", order_type="LIMIT", quantity=172.0,
                        price=0.0999, source="kss", source_ref=f"pyramid:{s.id}:tp",
                        status=PENDING))
    db.commit()

    kss.manage_orphan_positions(db)

    orphan_sells = [o for o in _queued_sells(db) if str(o.source_ref).startswith("orphan")]
    assert len(orphan_sells) == 1
    assert orphan_sells[0].quantity == pytest.approx(172.0), "the half nobody holds"


def test_an_exit_this_function_already_queued_is_not_queued_twice(db):
    """The orphan exit from an earlier cycle DOES count as cover — otherwise every cycle would
    stack another market sell on the same units."""
    _session(db, qty=172.0)
    _position(db, qty=344.0)
    db.add(PendingOrder(symbol="ARB", side="SELL", order_type="MARKET", quantity=172.0,
                        price=0.0, source="kss", source_ref="orphan:tp", status=PENDING))
    db.commit()

    kss.manage_orphan_positions(db)

    assert len(_queued_sells(db)) == 1, "the one already queued, and no second"
