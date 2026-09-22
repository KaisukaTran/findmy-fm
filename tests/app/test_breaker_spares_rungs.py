"""The circuit breaker stops NEW risk, not the ladder of a session already running.

Measured and owner-approved (2026-09-21): blocking DCA rung buys during a crash makes
drawdown WORSE at every capital level — the ladder recovering the average cost basis IS the
recovery mechanism, not new risk. With `breaker_blocks_ladder_rungs` off (the default), a
freeze no longer blocks a DCA-ladder rung (wave >= 1) of a still-active `dca_down` session on
any automated path — `auto_fill_due_orders`, `approve_order`, resting placement, or the
scheduler. It still blocks everything else a freeze always blocked: wave 0 (new capital into a
symbol), a Pyramid-UP add/defensive rung (anti-martingale — the opposite risk shape), a new
session open (scanner, untouched), and any manual/other automated BUY. A SELL is never gated by
the freeze at all, on any path, with or without the knob. `breaker_blocks_ladder_rungs=True`
restores the exact legacy behaviour: a freeze blocks every automated BUY.

`orders.freeze_blocks(db, order)` is the single place this rule lives; every site above calls
it instead of `runtime.is_frozen` directly.
"""

from __future__ import annotations

import pytest

from app import execution, orders, runtime
from app.clock import utcnow
from app.config import settings
from app.models import (
    EXECUTED,
    PENDING,
    SESSION_ACTIVE,
    SESSION_STOPPED,
    KssSession,
    PendingOrder,
)

# --- fixtures ----------------------------------------------------------------------------


def _session(db, *, symbol="SOL", strategy_mode="dca_down", status=SESSION_ACTIVE) -> KssSession:
    row = KssSession(
        symbol=symbol, entry_price=10.0, distance_pct=4.0, max_waves=10, isolated_fund=1000.0,
        tp_pct=5.0, timeout_x_min=60, gap_y_min=5, status=status, strategy_mode=strategy_mode,
        current_wave=1, avg_price=10.0, total_filled_qty=1.0, total_cost=10.0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _rung(db, *, session_id: int, wave: int, symbol="SOL", side="BUY", price=9.0,
          quantity=1.0, order_type="LIMIT") -> PendingOrder:
    o = PendingOrder(
        symbol=symbol, side=side, order_type=order_type, quantity=quantity, price=price,
        source="kss", source_ref=f"pyramid:{session_id}:wave:{wave}", status=PENDING,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


class _Venue:
    """Records resting placements and reports every placement as resting (never a fill), so
    the resting-model tests exercise only the freeze gate, not the fill path."""

    def __init__(self):
        self.placed: list[dict] = []

    def place(self, pair, side, quantity, price, order_type, maker_orders=None,
              client_order_id=None):
        self.placed.append({"pair": pair, "side": side, "quantity": quantity, "price": price})
        return {"raw_id": f"X{len(self.placed)}", "status": "open", "price": 0.0,
                "quantity": 0.0, "fee": 0.0}


# --- freeze_blocks: unit-level, every shape --------------------------------------------


def test_not_frozen_never_blocks_anything(db):
    row = _session(db)
    buy = _rung(db, session_id=row.id, wave=1)
    assert runtime.is_frozen(db) is False
    assert orders.freeze_blocks(db, buy) is False


def test_a_sell_is_never_blocked_even_frozen(db):
    row = _session(db)
    sell = _rung(db, session_id=row.id, wave=1, side="SELL", order_type="MARKET")
    runtime.freeze(db, "test")
    assert orders.freeze_blocks(db, sell) is False


def test_knob_true_blocks_every_buy_regardless_of_shape(db, monkeypatch):
    monkeypatch.setattr(settings, "breaker_blocks_ladder_rungs", True)
    row = _session(db)
    rung = _rung(db, session_id=row.id, wave=1)
    runtime.freeze(db, "test")
    assert orders.freeze_blocks(db, rung) is True


def test_knob_false_spares_a_ladder_rung_of_an_active_dca_down_session(db, monkeypatch):
    monkeypatch.setattr(settings, "breaker_blocks_ladder_rungs", False)
    row = _session(db, strategy_mode="dca_down", status=SESSION_ACTIVE)
    rung = _rung(db, session_id=row.id, wave=1)
    runtime.freeze(db, "test")
    assert orders.freeze_blocks(db, rung) is False


def test_wave_zero_is_new_risk_and_stays_blocked(db, monkeypatch):
    monkeypatch.setattr(settings, "breaker_blocks_ladder_rungs", False)
    row = _session(db)
    wave0 = _rung(db, session_id=row.id, wave=0)
    runtime.freeze(db, "test")
    assert orders.freeze_blocks(db, wave0) is True


def test_pyramid_up_add_is_new_risk_and_stays_blocked(db, monkeypatch):
    """Same source_ref SHAPE as a dca_down rung (pyramid:{id}:wave:{k}, k>=1) — only the
    session's own strategy_mode tells them apart."""
    monkeypatch.setattr(settings, "breaker_blocks_ladder_rungs", False)
    row = _session(db, strategy_mode="pyramid_up")
    add = _rung(db, session_id=row.id, wave=1, order_type="MARKET")
    runtime.freeze(db, "test")
    assert orders.freeze_blocks(db, add) is True


def test_a_rung_of_an_ended_session_stays_blocked(db, monkeypatch):
    monkeypatch.setattr(settings, "breaker_blocks_ladder_rungs", False)
    row = _session(db, status=SESSION_STOPPED)
    rung = _rung(db, session_id=row.id, wave=1)
    runtime.freeze(db, "test")
    assert orders.freeze_blocks(db, rung) is True


def test_a_rung_of_a_tp_triggered_session_stays_blocked(db, monkeypatch):
    """`_active_dca_ladder_session` requires SESSION_ACTIVE specifically — deliberately
    stricter than `session_still_going` (which also passes PENDING/TP_TRIGGERED): a session on
    its way to TP is not the running ladder this exemption exists for, and buying more into it
    while frozen is new risk, not the ladder's own recovery mechanism."""
    from app.models import SESSION_TP_TRIGGERED

    monkeypatch.setattr(settings, "breaker_blocks_ladder_rungs", False)
    row = _session(db, status=SESSION_TP_TRIGGERED)
    rung = _rung(db, session_id=row.id, wave=1)
    runtime.freeze(db, "test")
    assert orders.freeze_blocks(db, rung) is True


def test_a_rung_of_a_pending_session_stays_blocked(db, monkeypatch):
    from app.models import SESSION_PENDING

    monkeypatch.setattr(settings, "breaker_blocks_ladder_rungs", False)
    row = _session(db, status=SESSION_PENDING)
    rung = _rung(db, session_id=row.id, wave=1)
    runtime.freeze(db, "test")
    assert orders.freeze_blocks(db, rung) is True


def test_a_manual_buy_stays_blocked(db, monkeypatch):
    monkeypatch.setattr(settings, "breaker_blocks_ladder_rungs", False)
    manual = PendingOrder(symbol="SOL", side="BUY", order_type="MARKET", quantity=1.0,
                          price=0.0, source="manual", source_ref=None, status=PENDING)
    db.add(manual)
    db.commit()
    db.refresh(manual)
    runtime.freeze(db, "test")
    assert orders.freeze_blocks(db, manual) is True


# --- auto_fill_due_orders: touch model + sampled-price model ---------------------------


def _paper(monkeypatch, *, prices: dict, candles: dict | None = None, touch=False):
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    monkeypatch.setattr(orders, "get_current_prices", lambda syms: {s: prices.get(s, 0.0) for s in syms})
    monkeypatch.setattr(settings, "paper_fill_touch_1m", touch)
    if touch:
        from app import scanner

        def _prefetch(exchange_id, symbols, timeframe, limit):
            return {s: (candles.get(s, []) if candles else [], False) for s in symbols}, False

        monkeypatch.setattr(scanner, "_prefetch_candles", _prefetch)
        monkeypatch.setattr(settings, "paper_fill_needs_trade_through", True)


def test_frozen_rung_fills_on_sampled_price_model(db, monkeypatch):
    _paper(monkeypatch, prices={"SOL": 9.0}, touch=False)
    row = _session(db)
    rung = _rung(db, session_id=row.id, wave=1, price=9.0)
    runtime.freeze(db, "test")

    assert orders.auto_fill_due_orders(db) == [rung.id]
    db.refresh(rung)
    assert rung.status == EXECUTED


def test_frozen_rung_fills_on_touch_model(db, monkeypatch):
    now_ms = int(utcnow().timestamp() * 1000)
    candle = {"ts": now_ms - 60_000, "open": 9.3, "high": 9.4, "low": 8.9, "close": 9.0,
              "volume": 1.0}
    _paper(monkeypatch, prices={"SOL": 9.0}, candles={"SOL": [candle]}, touch=True)
    row = _session(db)
    rung = _rung(db, session_id=row.id, wave=1, price=9.0)
    rung.created_at = utcnow().__class__(2020, 1, 1)  # long before the candle, so it "touched"
    db.commit()
    runtime.freeze(db, "test")

    assert orders.auto_fill_due_orders(db) == [rung.id]
    db.refresh(rung)
    assert rung.status == EXECUTED


def test_frozen_wave_zero_stays_pending(db, monkeypatch):
    _paper(monkeypatch, prices={"SOL": 9.0}, touch=False)
    row = _session(db)
    wave0 = _rung(db, session_id=row.id, wave=0, price=9.0)
    runtime.freeze(db, "test")

    assert orders.auto_fill_due_orders(db) == []
    db.refresh(wave0)
    assert wave0.status == PENDING


def test_frozen_new_session_open_is_out_of_scope_here_but_wave_zero_proves_it_blocks(db, monkeypatch):
    """A brand-new session's very first order IS wave 0 — already covered above. Opening a new
    session at all is the scanner's job (app/scanner.py), untouched by this change and still
    gated by its own `runtime.is_frozen` check; not exercised via `auto_fill_due_orders`."""


def test_frozen_pyramid_up_add_stays_pending(db, monkeypatch):
    _paper(monkeypatch, prices={"SOL": 9.0}, touch=False)
    row = _session(db, strategy_mode="pyramid_up")
    add = _rung(db, session_id=row.id, wave=1, price=9.0, order_type="MARKET")
    runtime.freeze(db, "test")

    assert orders.auto_fill_due_orders(db) == []
    db.refresh(add)
    assert add.status == PENDING


def test_paper_resting_tp_sell_fills_while_frozen(db, monkeypatch):
    _paper(monkeypatch, prices={"SOL": 11.0}, touch=False)
    row = _session(db)
    tp = _rung(db, session_id=row.id, wave=0, symbol="SOL", side="SELL", price=10.0)
    tp.source_ref = f"pyramid:{row.id}:tp"
    db.commit()
    runtime.freeze(db, "test")

    assert orders.auto_fill_due_orders(db) == [tp.id]
    db.refresh(tp)
    assert tp.status == EXECUTED


def test_knob_true_restores_legacy_freeze_blocks_the_rung_too(db, monkeypatch):
    monkeypatch.setattr(settings, "breaker_blocks_ladder_rungs", True)
    _paper(monkeypatch, prices={"SOL": 9.0}, touch=False)
    row = _session(db)
    rung = _rung(db, session_id=row.id, wave=1, price=9.0)
    runtime.freeze(db, "test")

    assert orders.auto_fill_due_orders(db) == []
    db.refresh(rung)
    assert rung.status == PENDING


def test_manual_dashboard_buy_is_never_blocked_by_the_freeze(db, monkeypatch):
    """A human approval always bypasses the freeze, independent of `freeze_blocks`."""
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms, **kw: {"SOL": 100.0})
    row = _session(db)
    wave0 = _rung(db, session_id=row.id, wave=0, price=100.0)
    runtime.freeze(db, "test")

    fill = orders.approve_order(db, wave0.id, reviewer="dashboard")
    assert fill.side == "BUY"


def test_auto_reviewer_is_blocked_for_wave_zero_but_not_for_a_ladder_rung(db, monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: False)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms, **kw: {"SOL": 9.0})
    row = _session(db)
    wave0 = _rung(db, session_id=row.id, wave=0, price=9.0)
    rung = _rung(db, session_id=row.id, wave=1, price=9.0)
    runtime.freeze(db, "test")

    with pytest.raises(ValueError, match="frozen"):
        orders.approve_order(db, wave0.id, reviewer="auto-trader")

    fill = orders.approve_order(db, rung.id, reviewer="auto-trader")
    assert fill.side == "BUY"


# --- touch-window invariant: blocked BUY resets, allowed rung does not -----------------


def test_blocked_buys_touch_window_resets_allowed_rungs_does_not(db, monkeypatch):
    now_ms = int(utcnow().timestamp() * 1000)
    candle = {"ts": now_ms - 60_000, "open": 9.3, "high": 9.4, "low": 8.9, "close": 9.0,
              "volume": 1.0}
    _paper(monkeypatch, prices={"SOL": 9.0}, candles={"SOL": [candle]}, touch=True)
    row = _session(db)
    old = utcnow().__class__(2020, 1, 1)
    wave0 = _rung(db, session_id=row.id, wave=0, price=9.0)
    rung = _rung(db, session_id=row.id, wave=1, price=9.0)
    wave0.created_at = old
    rung.created_at = old
    db.commit()
    runtime.freeze(db, "test")

    assert orders.auto_fill_due_orders(db) == [rung.id]
    db.refresh(wave0)
    db.refresh(rung)
    assert wave0.status == PENDING
    assert wave0.created_at > old, "the blocked wave-0 BUY's touch window must restart"
    assert rung.status == EXECUTED, "the allowed rung must have filled, not merely rested"


# --- resting placement (live maker model) ----------------------------------------------


def test_resting_placement_spares_the_ladder_rung_but_not_pyramid_up(db, monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    venue = _Venue()
    monkeypatch.setattr(execution, "place_live_order", venue.place)
    settings.maker_orders = True
    settings.auto_trade = True
    dca = _session(db, symbol="SOL", strategy_mode="dca_down")
    up = _session(db, symbol="ETH", strategy_mode="pyramid_up")
    ladder_rung = _rung(db, session_id=dca.id, wave=1, symbol="SOL", price=9.0)
    up_add = _rung(db, session_id=up.id, wave=1, symbol="ETH", price=1500.0)
    runtime.freeze(db, "test")

    out = orders.sync_resting_orders(db)

    assert out["placed"] == 1
    assert [p["pair"] for p in venue.placed] == ["SOL/USDT"]
    db.refresh(ladder_rung)
    db.refresh(up_add)
    assert ladder_rung.exchange_order_id is not None
    assert up_add.exchange_order_id is None
