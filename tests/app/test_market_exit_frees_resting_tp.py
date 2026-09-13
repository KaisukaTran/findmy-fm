"""A market exit must free the coins its own resting take-profit has locked.

Live, 2026-09-12: three risk exits (PUMP order 197, APT 264, PENDLE 311) sat PENDING for
days. Every 90 s the position guard force-filled them and the venue answered -2010
"insufficient balance" — the session's resting take-profit (a LIMIT SELL for the same
quantity) was locking the position, nothing cancelled it first, and the session stayed
ACTIVE so `sync_resting_tp` never retired it either. PUMP drifted from −1.8% to −18% with an
exit "in flight" the whole time. A second cause on PUMP: after a testnet reset the wallet held
18,446 coins against a booked 26,468, and a full-size sell is refused forever.

Everything here is offline: live_enabled, the venue calls and the balance read are stubbed.
"""

import pytest

from app import execution, orders
from app.config import settings
from app.models import EXECUTED, PENDING, REJECTED, AuditLog, PendingOrder, Position


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


class _Venue:
    """Records every venue call in ORDER so a test can assert what came first."""

    def __init__(self, *, free=None, locked=0.0, cancel_error=None):
        self.events: list[tuple] = []
        self.free = free
        self.locked = locked
        self._cancel_error = cancel_error

    def place(self, pair, side, quantity, price, order_type, maker_orders=None,
              client_order_id=None):
        self.events.append(("place", side, order_type, quantity))
        return {"raw_id": "M1", "status": "closed", "price": 10.0, "quantity": quantity,
                "fee": 0.0}

    def cancel(self, pair, order_id):
        self.events.append(("cancel", order_id))
        if self._cancel_error:
            raise self._cancel_error

    def fetch(self, pair, order_id):
        return {"status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0,
                "raw_id": order_id}

    def balance(self, asset):
        self.events.append(("balance", asset))
        return None if self.free is None else (self.free, self.locked)

    @property
    def placed(self):
        return [e for e in self.events if e[0] == "place"]

    @property
    def cancelled(self):
        return [e[1] for e in self.events if e[0] == "cancel"]


def _live(monkeypatch, venue: _Venue, *, maker=True) -> _Venue:
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "place_live_order", venue.place)
    monkeypatch.setattr(execution, "cancel_live_order", venue.cancel)
    monkeypatch.setattr(execution, "fetch_live_order", venue.fetch)
    monkeypatch.setattr(execution, "fetch_asset_balance", venue.balance)
    monkeypatch.setattr(orders, "get_current_prices", lambda syms: dict.fromkeys(syms, 10.0))
    settings.maker_orders = maker
    settings.auto_trade = True
    return venue


def _row(db, **kw) -> PendingOrder:
    defaults = {
        "symbol": "SOL", "side": "SELL", "order_type": "MARKET", "quantity": 26468.0,
        "price": 0.0, "source": "kss", "source_ref": "pyramid:47:trailing", "status": PENDING,
    }
    defaults.update(kw)
    o = PendingOrder(**defaults)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _resting_tp(db, sid=47, qty=26468.0) -> PendingOrder:
    return _row(db, order_type="LIMIT", price=12.0, quantity=qty, source_ref=f"pyramid:{sid}:tp",
                exchange_order_id="TP1", exchange_status="open")


def _audits(db, action):
    return db.query(AuditLog).filter(AuditLog.action == action).all()


# --- the resting take-profit comes off the book first ------------------------------------


def test_exit_pulls_the_sibling_tp_off_the_book_before_it_is_placed(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(free=26468.0))
    tp = _resting_tp(db)
    exit_order = _row(db)

    fill = orders._live_execute(db, exit_order)

    assert venue.cancelled == ["TP1"]
    cancel_at = venue.events.index(("cancel", "TP1"))
    place_at = next(i for i, e in enumerate(venue.events) if e[0] == "place")
    assert cancel_at < place_at, "the lock must be released BEFORE the exit is sent"
    assert venue.placed == [("place", "SELL", "MARKET", 26468.0)]
    assert fill.quantity == 26468.0
    db.refresh(tp)
    assert tp.status == REJECTED
    assert tp.reject_reason == "resting-tp: superseded by market exit"
    assert tp.exchange_order_id is None, "a clean cancel unlinks the TP"
    assert len(_audits(db, "tp_retired_for_exit")) == 1


def test_a_refused_tp_cancel_never_holds_the_exit(db, monkeypatch):
    """Exits are never gated: the cancel failing is logged, the TP row is retired (link kept so
    sync_resting_orders keeps trying), and the exit still goes out."""
    venue = _live(monkeypatch, _Venue(free=26468.0, cancel_error=RuntimeError("venue down")))
    tp = _resting_tp(db)
    exit_order = _row(db)

    orders._live_execute(db, exit_order)

    assert len(venue.placed) == 1
    db.refresh(tp)
    assert tp.status == REJECTED
    assert tp.exchange_order_id == "TP1", "a refused cancel keeps the link for the next sweep"


def test_the_guard_path_end_to_end_marks_the_exit_executed(db, monkeypatch):
    """Through approve_order (what the 90 s guard calls), not just _live_execute."""
    venue = _live(monkeypatch, _Venue(free=26468.0))
    _resting_tp(db)
    exit_order = _row(db)

    orders.approve_order(db, exit_order.id, reviewer="guard")

    db.refresh(exit_order)
    assert exit_order.status == EXECUTED
    assert venue.cancelled == ["TP1"]


# --- the exit is sized to what the venue really holds --------------------------------------


def test_exit_is_clamped_to_the_venue_free_balance(db, monkeypatch):
    """PUMP after the testnet reset: booked 26,468, wallet 18,446, no open order left."""
    venue = _live(monkeypatch, _Venue(free=18446.0))
    exit_order = _row(db)

    fill = orders._live_execute(db, exit_order)

    assert venue.placed == [("place", "SELL", "MARKET", 18446.0)]
    assert fill.quantity == 18446.0
    db.refresh(exit_order)
    assert exit_order.quantity == 18446.0
    (row,) = _audits(db, "exit_qty_clamped")
    assert '"requested": 26468.0' in row.detail and '"venue_free": 18446.0' in row.detail


def test_a_balance_the_venue_cannot_report_keeps_the_booked_size(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(free=None))
    exit_order = _row(db)

    orders._live_execute(db, exit_order)

    assert venue.placed == [("place", "SELL", "MARKET", 26468.0)]
    assert _audits(db, "exit_qty_clamped") == []


def test_more_on_the_venue_than_booked_sells_only_the_booked_size(db, monkeypatch):
    """Free balance can include coins that belong to OTHER sessions — never sell those."""
    venue = _live(monkeypatch, _Venue(free=40000.0))
    exit_order = _row(db)

    orders._live_execute(db, exit_order)

    assert venue.placed == [("place", "SELL", "MARKET", 26468.0)]


def test_coins_locked_elsewhere_are_retried_not_written_off(db, monkeypatch):
    """free 0 but locked > 0: something else holds the coins — raise, stay PENDING, retry."""
    venue = _live(monkeypatch, _Venue(free=0.0, locked=26468.0))
    exit_order = _row(db)

    with pytest.raises(ValueError, match="locked in open orders"):
        orders.approve_order(db, exit_order.id, reviewer="guard")

    assert venue.placed == []
    db.refresh(exit_order)
    assert exit_order.status == PENDING


def test_phantom_inventory_is_written_off_and_the_order_is_not_retried(db, monkeypatch):
    """Live 2026-09-13: after the real 18,446 PUMP were sold the book still carried 8,022 the
    venue never had (testnet reset), and an orphan:sl sweep for them would have looped -2010
    forever. Nothing free, nothing locked → write the position off, reject the order for good."""
    venue = _live(monkeypatch, _Venue(free=0.0, locked=0.0))
    db.add(Position(symbol="SOL", quantity=8022.9, avg_entry_price=0.0043, total_cost=35.0,
                    realized_pnl=-6.65))
    db.commit()
    exit_order = _row(db, quantity=8022.9, source_ref="orphan:sl")

    with pytest.raises(orders.ExitUnsellable):
        orders.approve_order(db, exit_order.id, reviewer="guard")

    assert venue.placed == []
    db.refresh(exit_order)
    assert exit_order.status == REJECTED
    assert "phantom" in exit_order.reject_reason
    pos = db.query(Position).filter(Position.symbol == "SOL").one()
    assert pos.quantity == 0.0 and pos.total_cost == 0.0
    assert pos.realized_pnl == -6.65, "history is kept; only the fiction is removed"
    (row,) = _audits(db, "phantom_inventory_writeoff")
    assert '"position_qty": 8022.9' in row.detail


def test_an_orphan_sweep_is_also_clamped_to_the_venue(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(free=100.0))
    exit_order = _row(db, quantity=150.0, source_ref="orphan:tp")

    orders._live_execute(db, exit_order)

    assert venue.placed == [("place", "SELL", "MARKET", 100.0)]
    assert venue.cancelled == [], "an orphan has no session, so no sibling TP to retire"


# --- nothing else enters the new path -------------------------------------------------------


def test_paper_never_touches_the_venue(db, monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    monkeypatch.setattr(execution, "fetch_asset_balance",
                        lambda asset: pytest.fail("paper must not read a venue balance"))
    monkeypatch.setattr(execution, "cancel_live_order",
                        lambda *a: pytest.fail("paper must not cancel on a venue"))
    monkeypatch.setattr(orders, "get_current_prices", lambda syms: dict.fromkeys(syms, 10.0))
    settings.maker_orders = True
    _resting_tp(db)
    exit_order = _row(db)

    fill = orders.approve_order(db, exit_order.id, reviewer="guard")

    assert fill.side == "SELL"


def test_legacy_live_model_is_unchanged(db, monkeypatch):
    """Live with maker off has no resting TP to free; the exit goes out exactly as before."""
    venue = _live(monkeypatch, _Venue(free=1.0), maker=False)
    exit_order = _row(db)

    orders._live_execute(db, exit_order)

    assert venue.placed == [("place", "SELL", "MARKET", 26468.0)]
    assert ("balance", "SOL") not in venue.events


def test_the_resting_tp_itself_is_not_a_risk_exit(db, monkeypatch):
    """Force-filling the TP row keeps today's cancel-own-order-then-place path and never reads
    a balance (that path is `test_live_execute_cancels_the_resting_order_first`)."""
    venue = _live(monkeypatch, _Venue(free=1.0))
    tp = _resting_tp(db)

    orders._live_execute(db, tp)

    assert venue.cancelled == ["TP1"]
    assert ("balance", "SOL") not in venue.events


def test_a_buy_never_enters_the_exit_path(db, monkeypatch):
    venue = _live(monkeypatch, _Venue(free=0.0))
    _resting_tp(db)
    buy = _row(db, side="BUY", source_ref="pyramid:47:wave:0", quantity=1.0)

    orders._live_execute(db, buy)

    assert venue.cancelled == []
    assert ("balance", "SOL") not in venue.events


# --- the balance helper fails soft ----------------------------------------------------------


def test_fetch_asset_balance_returns_none_when_the_venue_errors(monkeypatch):
    class _Ex:
        def fetch_balance(self):
            raise RuntimeError("timeout")

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    assert execution.fetch_asset_balance("SOL") is None
    assert execution.fetch_free_balance("SOL") is None


def test_fetch_asset_balance_reads_free_and_locked(monkeypatch):
    class _Ex:
        def fetch_balance(self):
            return {"APT": {"free": 774.07, "used": 1376.19, "total": 2150.26}}

    monkeypatch.setattr(execution, "_client", lambda: _Ex())
    assert execution.fetch_asset_balance("APT") == (774.07, 1376.19)
    assert execution.fetch_free_balance("APT") == 774.07
    assert execution.fetch_asset_balance("SOL") == (0.0, 0.0)
