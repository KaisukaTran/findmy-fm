"""
Two seams found by an arming-transition audit on 2026-09-07.

GAP 1 — pausing BUYING silently paused TAKING PROFIT.
`orders.sync_resting_orders` cancels ungated (its own comment: "a rejected or timed-out order
must always come off the book") but places everything under `if settings.auto_trade:`, and the
query it runs has no `side` filter — so a resting take-profit cancelled for re-pricing was never
put back while auto-trade was off. Protective exits survive (the ~90s guard force-fills MARKET
sl/trail/deadline/crash exits and `approve_order` is not gated), so this is not a safety hole;
what disappears is the profit-taking leg, which is **25 of the 30 exits this book has ever
made**. CLAUDE.md: "Exits are never gated." A pause on new risk is not a pause on realising it.

This became durable on 2026-09-06: auto-trade used to be re-armed by the full-auto cascade on
every restart, so the condition healed itself; it now persists exactly as the operator set it.

GAP 2 — a late rung fill rebuilt the ladder an armed session had just thrown away.
`_cancel_pending_waves` rejects rows in the DB; the venue cancel goes out on a later pass
(~1.5s measured on INJ, up to 90s if the cycle aborts, unbounded across a process death). A rung
that fills inside that window runs the auto-chain, and `_queue_wave_if_above_sl` had no
`trail_active` check — while its sibling `_rearm_dead_ladders` has exactly that guard, with the
reasoning written out: re-queueing "would average down into a position the strategy has switched
to riding out". Same argument, written in one place and forgotten in the other.
"""

from __future__ import annotations

import pytest

from app import execution, models, orders
from app.config import settings
from app.kss import service
from app.models import PENDING, KssSession, PendingOrder

# --- GAP 1: an exit is placed even when auto-trade is off ----------------------------------


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


class _Venue:
    def __init__(self):
        self.placed: list[dict] = []

    def place(self, pair, side, quantity, price, order_type,
              maker_orders=None, client_order_id=None):
        self.placed.append({"side": side, "price": price})
        return {"raw_id": "X1", "status": "open", "price": price, "quantity": quantity, "fee": 0.0}

    def cancel(self, pair, order_id):
        pass

    def fetch(self, pair, order_id):
        return {"status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0, "raw_id": "X1"}


@pytest.fixture
def venue(monkeypatch):
    v = _Venue()
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "place_live_order", v.place)
    monkeypatch.setattr(execution, "cancel_live_order", v.cancel)
    monkeypatch.setattr(execution, "fetch_live_order", v.fetch)
    monkeypatch.setattr(settings, "maker_orders", True)
    return v


def _session(db) -> KssSession:
    row = KssSession(
        symbol="SOL", entry_price=10.0, distance_pct=3.0, max_waves=3, isolated_fund=300.0,
        tp_pct=5.0, timeout_x_min=60, gap_y_min=5, status=models.SESSION_ACTIVE,
        current_wave=1, avg_price=10.0, total_filled_qty=3.0, total_cost=30.0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _order(db, sid, side, ref_suffix, price):
    o = PendingOrder(symbol="SOL", side=side, order_type="LIMIT", quantity=1.0, price=price,
                     source="kss", source_ref=f"pyramid:{sid}:{ref_suffix}", status=PENDING)
    db.add(o)
    db.commit()
    return o


class TestAnExitIsPlacedRegardlessOfAutoTrade:
    def test_the_take_profit_rests_with_auto_trade_off(self, db, venue, monkeypatch):
        monkeypatch.setattr(settings, "auto_trade", False)
        row = _session(db)
        _order(db, row.id, "SELL", "tp", 10.5)
        orders.sync_resting_orders(db)
        assert [p["side"] for p in venue.placed] == ["SELL"], (
            "the profit-taking exit was left off the book because new buying was paused")

    def test_a_buy_rung_is_still_held_back_with_auto_trade_off(self, db, venue, monkeypatch):
        # The switch must keep doing what it says: no NEW risk.
        monkeypatch.setattr(settings, "auto_trade", False)
        row = _session(db)
        _order(db, row.id, "BUY", "wave:2", 9.7)
        orders.sync_resting_orders(db)
        assert venue.placed == []

    def test_both_rest_when_auto_trade_is_on(self, db, venue, monkeypatch):
        monkeypatch.setattr(settings, "auto_trade", True)
        row = _session(db)
        _order(db, row.id, "SELL", "tp", 10.5)
        _order(db, row.id, "BUY", "wave:2", 9.7)
        orders.sync_resting_orders(db)
        assert sorted(p["side"] for p in venue.placed) == ["BUY", "SELL"]


# --- GAP 2: an armed session does not get its ladder back ----------------------------------


class TestAnArmedSessionRefusesANewRung:
    @staticmethod
    def _pyramid_for(row):
        py = service._to_pyramid(row)
        py.total_cost = row.total_cost
        return py

    def _try_queue(self, db, row):
        order_dict = {
            "symbol": "SOL", "side": "BUY", "quantity": 1.0, "price": 9.7,  # above the 8% SL floor (9.2)
            "order_type": "LIMIT", "source_ref": f"pyramid:{row.id}:wave:2",
            "strategy_name": "Pyramid_SOL", "note": "rung",
        }
        return service._queue_wave_if_above_sl(
            db, self._pyramid_for(row), row.id, "SOL", order_dict)

    def test_a_rung_is_refused_once_the_session_is_armed(self, db, monkeypatch):
        monkeypatch.setattr(service, "_anchor_dca_price",
                            lambda db_, sid, sym, d, price, entry: price)
        row = _session(db)
        row.trail_active = True
        row.trail_sl_price = 10.2
        db.commit()
        assert self._try_queue(db, row) is False
        assert db.query(models.KssWave).filter_by(session_id=row.id).count() == 0
        assert db.query(models.AuditLog).filter_by(action="wave_after_arm").count() == 1

    def test_an_unarmed_session_still_gets_its_rung(self, db, monkeypatch):
        # The guard must not disable the ladder for everyone — this is the whole strategy.
        monkeypatch.setattr(service, "_anchor_dca_price",
                            lambda db_, sid, sym, d, price, entry: price)
        row = _session(db)
        assert self._try_queue(db, row) is True
        db.commit()          # the caller commits; the wave row is added, not flushed, here
        assert db.query(models.KssWave).filter_by(session_id=row.id).count() == 1
