"""Paper touch model (`paper_fill_touch_1m`): a resting LIMIT fills when a 1-minute candle
touches it, the way the venue fills a resting order — not when the 15-minute price sample
happens to sit below it.

Why: paper sampled the price once a cycle, so every touch that bounced back inside the cycle
was missed — the ladder under-filled and fast take-profits were lost, on exactly the mechanism
the 2026-09-13 deep-ladder experiment measures. Offline: prices and the 1m candle fetch are
stubbed; the knob is OFF by default so every other test keeps the sampled-price behaviour.
"""

from datetime import timedelta, timezone

import pytest

from app import execution, models, orders, scanner
from app.clock import utcnow
from app.config import settings
from app.kss import service
from app.models import EXECUTED, PENDING, AuditLog, KssSession, PendingOrder

_MIN = 60_000


def _c(ts_min_ago: int, o: float, h: float, lo: float, cl: float | None = None) -> dict:
    # Epoch from a UTC wall clock — `.timestamp()` on a naive datetime would assume LOCAL time.
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


def _rung(db, *, price=9.0, side="BUY", ref="pyramid:1:wave:1", minutes_ago=10) -> PendingOrder:
    o = PendingOrder(symbol="SOL", side=side, order_type="LIMIT", quantity=2.0, price=price,
                     source="kss", source_ref=ref, status=PENDING,
                     created_at=utcnow() - timedelta(minutes=minutes_ago))
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _fills(db):
    return db.query(models.Fill).all()


# --- the core fix: a touch inside the cycle fills the rung -------------------------------


def test_a_touch_that_bounced_back_still_fills_at_the_limit(db, monkeypatch):
    """Market is 10 now; five minutes ago a candle dipped to 8.9 through the 9.0 limit."""
    _paper(monkeypatch, prices={"SOL": 10.0},
           candles={"SOL": [_c(8, 9.5, 9.6, 9.3), _c(5, 9.3, 9.4, 8.9), _c(1, 9.8, 10.1, 9.7)]})
    rung = _rung(db, price=9.0)

    assert orders.auto_fill_due_orders(db) == [rung.id]

    (fill,) = _fills(db)
    assert fill.price == 9.0, "a maker fills at its limit, not at the current market"
    assert fill.slippage == 0.0
    assert fill.fee == pytest.approx(9.0 * 2.0 * settings.maker_fee_pct / 100)
    (row,) = db.query(AuditLog).filter(AuditLog.action == "paper_touch_fill").all()
    assert '"fill": 9.0' in row.detail


def test_no_touch_means_no_fill_even_if_a_candle_came_close(db, monkeypatch):
    _paper(monkeypatch, prices={"SOL": 10.0},
           candles={"SOL": [_c(5, 9.3, 9.4, 9.05), _c(1, 9.8, 10.1, 9.7)]})
    rung = _rung(db, price=9.0)

    assert orders.auto_fill_due_orders(db) == []
    db.refresh(rung)
    assert rung.status == PENDING


def test_a_gap_below_the_limit_fills_at_the_candle_open(db, monkeypatch):
    """B4 on paper: the bar opened at 8.5, under the 9.0 limit — price improvement."""
    _paper(monkeypatch, prices={"SOL": 10.0},
           candles={"SOL": [_c(6, 9.4, 9.5, 9.2), _c(5, 8.5, 8.7, 8.4), _c(1, 9.8, 10.1, 9.7)]})
    _rung(db, price=9.0)

    orders.auto_fill_due_orders(db)

    (fill,) = _fills(db)
    assert fill.price == 8.5


def test_candles_before_the_order_existed_do_not_count(db, monkeypatch):
    _paper(monkeypatch, prices={"SOL": 10.0},
           candles={"SOL": [_c(30, 9.5, 9.6, 8.0), _c(1, 9.8, 10.1, 9.7)]})
    _rung(db, price=9.0, minutes_ago=10)

    assert orders.auto_fill_due_orders(db) == []


# --- post-only is simulated ------------------------------------------------------------------


def test_a_rung_queued_below_the_market_waits_for_the_market_to_come_back_above(db, monkeypatch):
    """Queued while the market already sat under 9.0: the venue would reject the post-only
    placement. Candles that stay under the limit do not fill it; only after a candle opens
    above (order now resting) and a later dip trades through does it fill."""
    _paper(monkeypatch, prices={"SOL": 8.8},
           candles={"SOL": [_c(9, 8.7, 8.8, 8.6), _c(8, 8.8, 8.9, 8.7), _c(1, 8.8, 8.9, 8.7)]})
    rung = _rung(db, price=9.0, minutes_ago=10)

    # The sampled price IS under the limit — the old rule booked a marketable fill here. On the
    # venue the post-only order is rejected and re-queued; nothing fills until the market comes
    # back above the limit and dips through it again (next test).
    assert orders.auto_fill_due_orders(db) == []
    assert _fills(db) == []
    db.refresh(rung)
    assert rung.status == PENDING


def test_a_take_profit_under_the_market_is_not_a_fill(db, monkeypatch):
    """Paper PEPE 2026-09-13: entry mispriced 13% under the market, so the TP sat under the
    market too and the sampled-price fallback "filled" it there for a profit no venue pays."""
    _paper(monkeypatch, prices={"SOL": 11.0},
           candles={"SOL": [_c(3, 11.0, 11.1, 10.9), _c(1, 11.0, 11.1, 10.9)]})
    tp = _rung(db, side="SELL", price=10.5, ref="pyramid:1:tp", minutes_ago=5)

    assert orders.auto_fill_due_orders(db) == []
    db.refresh(tp)
    assert tp.status == PENDING


def test_resting_then_dip_fills_after_the_market_recovered(db, monkeypatch):
    _paper(monkeypatch, prices={"SOL": 9.6},
           candles={"SOL": [_c(9, 8.7, 8.8, 8.6), _c(6, 9.2, 9.4, 9.1), _c(3, 9.3, 9.3, 8.95),
                            _c(1, 9.5, 9.7, 9.4)]})
    rung = _rung(db, price=9.0, minutes_ago=10)

    assert orders.auto_fill_due_orders(db) == [rung.id]
    (fill,) = _fills(db)
    assert fill.price == 9.0


# --- queue-position haircut ------------------------------------------------------------------


def test_an_exact_touch_does_not_fill_when_trade_through_is_required(db, monkeypatch):
    _paper(monkeypatch, prices={"SOL": 10.0},
           candles={"SOL": [_c(5, 9.3, 9.4, 9.0), _c(1, 9.8, 10.1, 9.7)]}, strict=True)
    _rung(db, price=9.0)
    assert orders.auto_fill_due_orders(db) == []


def test_an_exact_touch_fills_when_trade_through_is_off(db, monkeypatch):
    _paper(monkeypatch, prices={"SOL": 10.0},
           candles={"SOL": [_c(5, 9.3, 9.4, 9.0), _c(1, 9.8, 10.1, 9.7)]}, strict=False)
    rung = _rung(db, price=9.0)
    assert orders.auto_fill_due_orders(db) == [rung.id]


# --- the take-profit rests as a LIMIT row and fills on a high ------------------------------


def _session(db) -> KssSession:
    row = KssSession(
        symbol="SOL", entry_price=10.0, distance_pct=4.0, max_waves=10, isolated_fund=3000.0,
        tp_pct=5.0, timeout_x_min=60, gap_y_min=5, status=models.SESSION_ACTIVE,
        current_wave=1, avg_price=10.0, total_filled_qty=3.0, total_cost=30.0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_paper_tp_rests_as_a_limit_row_and_is_not_sold_at_market_on_the_sample(db, monkeypatch):
    _paper(monkeypatch, prices={"SOL": 10.0}, candles={"SOL": []})
    row = _session(db)

    out = service.sync_resting_tp(db)
    assert out["queued"] == 1
    tp = db.query(PendingOrder).filter(PendingOrder.source_ref == f"pyramid:{row.id}:tp").one()
    assert tp.order_type == "LIMIT" and tp.price > 10.0

    # The 15-minute check sees the price above the target — under the touch model it must
    # NOT queue a second, MARKET take-profit for the same inventory.
    monkeypatch.setattr("app.market.get_current_prices", lambda syms, **kw: {"SOL": 11.0})
    monkeypatch.setattr("app.kss.service.get_current_prices", lambda syms, **kw: {"SOL": 11.0},
                        raising=False)
    service.manage_open_sessions(db)
    sells = db.query(PendingOrder).filter(PendingOrder.side == "SELL").all()
    assert [s.order_type for s in sells] == ["LIMIT"]


def test_a_high_that_touched_the_tp_completes_the_session_at_the_tp_price(db, monkeypatch):
    _paper(monkeypatch, prices={"SOL": 10.0}, candles={"SOL": []})
    row = _session(db)
    service.sync_resting_tp(db)
    tp = db.query(PendingOrder).filter(PendingOrder.source_ref == f"pyramid:{row.id}:tp").one()
    target = tp.price
    calls = _paper(monkeypatch, prices={"SOL": 10.0},
                   candles={"SOL": [_c(4, 10.2, 10.3, 10.1), _c(2, 10.4, target * 1.002, 10.3),
                                    _c(1, 10.1, 10.2, 10.0)]})
    tp.created_at = utcnow() - timedelta(minutes=6)
    db.commit()

    assert orders.auto_fill_due_orders(db) == [tp.id]

    db.refresh(tp)
    db.refresh(row)
    assert tp.status == EXECUTED
    assert row.status == models.SESSION_COMPLETED
    (fill,) = _fills(db)
    assert fill.price == pytest.approx(target)
    assert calls and calls[-1][0] == "1m"


def test_the_guard_does_not_force_fill_the_paper_resting_tp(db, monkeypatch):
    """On the venue the guard leaves the standing TP alone; the paper touch model is the same
    standing LIMIT, so force-filling it at the (lower) market would sell below the target."""
    _paper(monkeypatch, prices={"SOL": 10.0}, candles={"SOL": []})
    row = _session(db)
    service.sync_resting_tp(db)
    forced: list[int] = []
    monkeypatch.setattr(orders, "approve_order", lambda db_, oid, **kw: forced.append(oid))

    service.run_position_guard(db)

    assert forced == []
    assert row.status == models.SESSION_ACTIVE


# --- off by default, and never on live ------------------------------------------------------


def test_knob_off_keeps_the_sampled_price_model(db, monkeypatch):
    calls = _paper(monkeypatch, prices={"SOL": 10.0},
                   candles={"SOL": [_c(5, 9.3, 9.4, 8.9)]}, touch=False)
    _rung(db, price=9.0)

    assert orders.auto_fill_due_orders(db) == []
    assert calls == [], "no candle fetch when the model is off"
    assert service.sync_resting_tp(db)["queued"] == 0


def test_live_never_uses_the_touch_model(monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr(settings, "paper_fill_touch_1m", True)
    assert not orders.touch_model_active()
