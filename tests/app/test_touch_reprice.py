"""A paper (touch-model) resting LIMIT that is re-priced/re-sized IN PLACE must restart its
touch window at the re-price moment — treated exactly like a freshly placed venue order.

Why: `sync_resting_tp` cancel+replaces a session's resting take-profit in place
(`existing.price = price; existing.quantity = qty`) but historically kept the OLD
`created_at`. `_touch_fill_price`'s 1-minute-candle scan reaches back to (near) when the order
was created, so it could "fill" the NEW (lower) target against a candle that traded through it
BEFORE the re-price ever happened — a touch the market gave to a price that no longer existed by
the time the order was actually resting there.

Real example: HEI session 300 — a wave fill dragged the average down, `sync_resting_tp`
re-priced the resting TP to the new (lower) target, and the stale touch window let it "fill"
at +11.5% against a market that was nowhere near it. 29 such fills in a week were 22% of all
booked paper profit, and their true value was negative.

The fix is one helper, `orders.reprice_resting_order`, used at every site that mutates a
PENDING resting LIMIT's price/quantity in place under the touch model.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import execution, models, orders, runtime, scanner
from app.clock import utcnow
from app.config import settings
from app.kss import service
from app.models import EXECUTED, PENDING, KssSession, PendingOrder

_MIN = 60_000


def _c(ts_min_ago: int, o: float, h: float, lo: float, cl: float | None = None) -> dict:
    now_ms = int(utcnow().replace(tzinfo=timezone.utc).timestamp() * 1000)
    return {"ts": now_ms - ts_min_ago * _MIN, "open": o, "high": h, "low": lo,
            "close": cl if cl is not None else o, "volume": 1.0}


def _paper(monkeypatch, *, prices: dict, candles: dict, touch=True, strict=True):
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    monkeypatch.setattr(orders, "get_current_prices", lambda syms: {s: prices.get(s, 0.0) for s in syms})
    monkeypatch.setattr("app.market.get_current_prices", lambda syms, **kw: {s: prices.get(s, 0.0) for s in syms})
    calls: list[tuple] = []

    def _prefetch(exchange_id, symbols, timeframe, limit):
        calls.append((timeframe, tuple(symbols), limit))
        return {s: (candles.get(s, []), False) for s in symbols}, False

    monkeypatch.setattr(scanner, "_prefetch_candles", _prefetch)
    monkeypatch.setattr(settings, "paper_fill_touch_1m", touch)
    monkeypatch.setattr(settings, "paper_fill_needs_trade_through", strict)
    monkeypatch.setattr(settings, "maker_orders", False)
    monkeypatch.setattr(settings, "auto_trade", True)
    return calls


def _rung(db, *, symbol="SOL", price=9.0, side="BUY", ref="pyramid:1:wave:1", minutes_ago=10) -> PendingOrder:
    o = PendingOrder(symbol=symbol, side=side, order_type="LIMIT", quantity=2.0, price=price,
                     source="kss", source_ref=ref, status=PENDING,
                     created_at=utcnow() - timedelta(minutes=minutes_ago))
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _session(db, *, symbol="HEI", avg_price=10.0, tp_pct=5.0, total_filled_qty=3.0,
            total_cost=30.0) -> KssSession:
    row = KssSession(
        symbol=symbol, entry_price=avg_price, distance_pct=4.0, max_waves=10,
        isolated_fund=3000.0, tp_pct=tp_pct, timeout_x_min=60, gap_y_min=5,
        status=models.SESSION_ACTIVE, current_wave=1, avg_price=avg_price,
        total_filled_qty=total_filled_qty, total_cost=total_cost,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _fills(db):
    return db.query(models.Fill).all()


# --- the HEI shape: a re-price must restart the touch window ---------------------------------


def test_reprice_restarts_the_touch_window_hei_shape(db, monkeypatch):
    """A resting TP is queued at a HIGH target; before any candle is even evaluated against it,
    a wave fill drags the session average (and the TP target) DOWN and `sync_resting_tp`
    re-prices the SAME row in place. A candle that traded through the new, lower price BEFORE
    the re-price must not count as a touch of it."""
    _paper(monkeypatch, prices={"HEI": 0.16}, candles={"HEI": []})
    row = _session(db, avg_price=0.2, tp_pct=5.0, total_filled_qty=100.0, total_cost=20.0)
    out = service.sync_resting_tp(db)
    assert out["queued"] == 1
    tp = db.query(PendingOrder).filter(PendingOrder.source_ref == f"pyramid:{row.id}:tp").one()
    old_target = tp.price
    # Backdate as if it had rested for a while already.
    tp.created_at = utcnow() - timedelta(minutes=10)
    db.commit()

    # A rung fills and drags the average (and therefore the TP target) DOWN.
    row.avg_price = 0.152928
    row.total_filled_qty = 200.0
    row.total_cost = 200.0 * 0.152928
    db.commit()
    out2 = service.sync_resting_tp(db)
    assert out2["replaced"] == 1
    db.refresh(tp)
    new_target = tp.price
    assert new_target < old_target, "the fill must have moved the target down"
    assert tp.created_at > utcnow() - timedelta(seconds=5), \
        "the re-price must restart the touch window"

    # An early candle (well before the re-price) opened BELOW the new, lower target and traded
    # through it — a genuine SELL fill if it were counted. Every candle AFTER the re-price stays
    # under the target (no fill).
    _paper(monkeypatch, prices={"HEI": new_target},
           candles={"HEI": [_c(8, new_target * 0.99, new_target * 1.06, new_target * 0.97),
                            _c(1, new_target * 0.95, new_target * 0.98, new_target * 0.94)]})

    assert orders.auto_fill_due_orders(db) == []
    db.refresh(tp)
    assert tp.status == PENDING, "a pre-re-price touch must not fill the re-priced order"


def test_a_later_touch_after_the_reprice_fills_at_the_new_limit(db, monkeypatch):
    """Same setup, but this time a candle AFTER the re-price genuinely opens below and trades
    through the new (lower) limit — it must fill."""
    _paper(monkeypatch, prices={"HEI": 0.16}, candles={"HEI": []})
    row = _session(db, avg_price=0.2, tp_pct=5.0, total_filled_qty=100.0, total_cost=20.0)
    service.sync_resting_tp(db)
    tp = db.query(PendingOrder).filter(PendingOrder.source_ref == f"pyramid:{row.id}:tp").one()
    tp.created_at = utcnow() - timedelta(minutes=10)
    db.commit()

    row.avg_price = 0.152928
    row.total_filled_qty = 200.0
    row.total_cost = 200.0 * 0.152928
    db.commit()
    service.sync_resting_tp(db)
    db.refresh(tp)
    new_target = tp.price
    # Simulate real time having passed since the re-price (which just restarted `created_at`),
    # then supply a fresh candle after that point — the scan starts at the NEXT full minute
    # after `created_at`, so a same-second "now" candle is not a reliable test of "later".
    tp.created_at = utcnow() - timedelta(minutes=3)
    db.commit()

    _paper(monkeypatch, prices={"HEI": new_target},
           candles={"HEI": [_c(1, new_target * 0.98, new_target * 1.02, new_target * 0.97)]})

    assert orders.auto_fill_due_orders(db) == [tp.id]
    db.refresh(tp)
    assert tp.status == EXECUTED
    (fill,) = _fills(db)
    assert fill.price == pytest.approx(new_target)


# --- cash-capped touch-model BUY must not back-fill a stale touch ----------------------------


def test_cash_capped_buy_does_not_backfill_a_stale_touch(db, monkeypatch):
    """A LIMIT BUY the market touched fails to execute (insufficient cash). It must not fill
    from that SAME stale touch once cash frees up — only a fresh touch, after the reset, may
    fill it."""
    _paper(monkeypatch, prices={"SOL": 10.0},
           candles={"SOL": [_c(8, 9.3, 9.4, 8.9), _c(1, 9.8, 10.1, 9.7)]})
    rung = _rung(db, price=9.0, minutes_ago=10)
    cash_ok = {"value": False}

    def _cash_cap(db_, o):
        if o.id == rung.id and not cash_ok["value"]:
            raise orders.InsufficientCashError("no cash")

    monkeypatch.setattr(orders, "_apply_cash_cap", _cash_cap)

    assert orders.auto_fill_due_orders(db) == []
    db.refresh(rung)
    assert rung.status == PENDING
    assert rung.created_at > utcnow() - timedelta(seconds=5), \
        "a failed touch-model BUY must restart its touch window"

    # Cash frees up, but the candles are unchanged — there has been no touch SINCE the reset.
    cash_ok["value"] = True
    assert orders.auto_fill_due_orders(db) == []
    db.refresh(rung)
    assert rung.status == PENDING

    # A genuinely new touch, after the reset, fills it. Simulate time having passed since the
    # reset (the scan starts at the NEXT full minute after `created_at`, so a same-second "now"
    # candle is not a reliable test of "later"), then supply a fresh candle after that point.
    rung.created_at = utcnow() - timedelta(minutes=5)
    db.commit()
    _paper(monkeypatch, prices={"SOL": 10.0},
           candles={"SOL": [_c(1, 9.3, 9.4, 8.9)]})

    assert orders.auto_fill_due_orders(db) == [rung.id]
    db.refresh(rung)
    assert rung.status == EXECUTED


def test_cash_capped_sell_is_never_made_harder_to_fill(db, monkeypatch):
    """Exits are never gated — a SELL that fails for some other reason must NOT have its touch
    window reset (only the BUY branch does)."""
    _paper(monkeypatch, prices={"SOL": 10.0},
           candles={"SOL": [_c(1, 9.8, 10.2, 9.7)]})
    tp = _rung(db, side="SELL", price=10.0, ref="pyramid:1:tp", minutes_ago=10)
    original_created_at = tp.created_at

    def _boom(db_, oid, reviewer=None, fill_price=None):
        raise ValueError("simulated venue reject")

    monkeypatch.setattr(orders, "approve_order", _boom)

    assert orders.auto_fill_due_orders(db) == []
    db.refresh(tp)
    assert tp.created_at == original_created_at


# --- a vetoed BUY must not back-fill a stale touch either -------------------------------------


def test_a_vetoed_buy_restarts_its_touch_window(db, monkeypatch):
    """A BUY skipped this cycle for `auto_veto` must not later fill from a touch that happened
    while it was vetoed — only a touch AFTER the veto lifts (and the reset) may fill it."""
    _paper(monkeypatch, prices={"SOL": 10.0},
           candles={"SOL": [_c(8, 9.3, 9.4, 8.9), _c(1, 9.8, 10.1, 9.7)]})
    rung = _rung(db, price=9.0, minutes_ago=10)
    rung.auto_veto = True
    db.commit()

    assert orders.auto_fill_due_orders(db) == []
    db.refresh(rung)
    assert rung.status == PENDING
    assert rung.created_at > utcnow() - timedelta(seconds=5), \
        "a vetoed BUY must restart its touch window"

    # The veto lifts, but the candles are unchanged — no touch has happened since the reset.
    rung.auto_veto = False
    db.commit()
    assert orders.auto_fill_due_orders(db) == []
    db.refresh(rung)
    assert rung.status == PENDING

    # A genuinely new touch, after the reset, fills it.
    rung.created_at = utcnow() - timedelta(minutes=5)
    db.commit()
    _paper(monkeypatch, prices={"SOL": 10.0}, candles={"SOL": [_c(1, 9.3, 9.4, 8.9)]})

    assert orders.auto_fill_due_orders(db) == [rung.id]


# --- the exception branch never resurrects a row that moved off PENDING ----------------------


def test_exception_branch_does_not_restamp_a_row_no_longer_pending(db, monkeypatch):
    """A row some earlier step already moved off PENDING (e.g. a sibling cancelled elsewhere in
    the same cycle) must not be restamped with a fresh touch window just because a later attempt
    to approve it then raises."""
    _paper(monkeypatch, prices={"SOL": 10.0},
           candles={"SOL": [_c(1, 9.3, 9.4, 8.9)]})
    rung = _rung(db, price=9.0, minutes_ago=10)
    original_created_at = rung.created_at

    def _boom(db_, oid, reviewer=None, fill_price=None):
        row = db_.get(PendingOrder, oid)
        row.status = models.REJECTED
        row.reject_reason = "cancelled by a sibling earlier this same cycle"
        db_.commit()
        raise ValueError("simulated post-cancel failure")

    monkeypatch.setattr(orders, "approve_order", _boom)

    assert orders.auto_fill_due_orders(db) == []
    db.refresh(rung)
    assert rung.status == models.REJECTED
    assert rung.created_at == original_created_at


# --- the helper itself ------------------------------------------------------------------------


def test_reprice_helper_restarts_created_at_under_the_touch_model(monkeypatch):
    monkeypatch.setattr(orders, "touch_model_active", lambda: True)
    o = PendingOrder(symbol="SOL", side="SELL", order_type="LIMIT", quantity=1.0, price=10.0,
                     status=PENDING, created_at=datetime(2020, 1, 1))

    orders.reprice_resting_order(o, price=11.0, quantity=2.0)

    assert o.price == 11.0
    assert o.quantity == 2.0
    assert o.created_at > datetime(2020, 1, 2)


def test_reprice_helper_leaves_created_at_alone_on_the_live_path(monkeypatch):
    monkeypatch.setattr(orders, "touch_model_active", lambda: False)
    fixed = datetime(2020, 1, 1)
    o = PendingOrder(symbol="SOL", side="SELL", order_type="LIMIT", quantity=1.0, price=10.0,
                     status=PENDING, created_at=fixed)

    orders.reprice_resting_order(o, price=11.0, quantity=2.0)

    assert o.price == 11.0
    assert o.quantity == 2.0
    assert o.created_at == fixed, "live resting orders' created_at also drives the resting timeout"
