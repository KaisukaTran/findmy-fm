"""Per-session deploy cap `max_session_deploy_usd` — the capital-preservation wall for going live
(Phase 0). A session's total DEPLOYED cost (filled + still-pending waves) may never exceed the cap;
any wave — auto-chain, manual DCA+, or the Telegram '➕' button — that would breach it is refused.
This is the missing 'top fix' from loss-cases.md (C deployed $162k unbounded; the ➕ button could
queue a ~$10k rung). 0 = off (paper wide-test unchanged).

Market is stubbed; no network.
"""

from __future__ import annotations

import pytest

from app import market, orders
from app.config import settings
from app.kss import service
from app.models import PENDING, SESSION_ACTIVE, KssSession, PendingOrder


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 0.0)     # default off
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": 95.0})


def _session(db, **kw):
    d = {"symbol": "AAA", "entry_price": 100.0, "distance_pct": 1.5, "max_waves": 6,
         "isolated_fund": 100000.0, "tp_pct": 4.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0,
         "status": SESSION_ACTIVE, "current_wave": 0, "avg_price": 100.0,
         "total_filled_qty": 1.0, "total_cost": 90.0, "sl_pct": 90.0}   # sl 90% → floor 10, rungs clear
    d.update(kw)
    s = KssSession(**d)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ---- headroom helper ----

def test_headroom_off_is_infinite(db):
    s = _session(db)
    assert service._session_deploy_headroom(db, s.id, s.total_cost) == float("inf")


def test_headroom_counts_deployed_and_pending(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 200.0)
    s = _session(db, total_cost=90.0)
    db.add(PendingOrder(symbol="AAA", side="BUY", quantity=10, price=3.0, order_type="LIMIT",
                        status=PENDING, source="kss", source_ref=f"pyramid:{s.id}:1"))  # $30 pending
    db.commit()
    # cap 200 − deployed 90 − pending 30 = 80
    assert service._session_deploy_headroom(db, s.id, s.total_cost) == pytest.approx(80.0)


def test_pending_notional_ignores_other_sessions_and_sells(db):
    s = _session(db)
    db.add(PendingOrder(symbol="AAA", side="BUY", quantity=10, price=2.0, order_type="LIMIT",
                        status=PENDING, source="kss", source_ref=f"pyramid:{s.id}:1"))     # counts $20
    db.add(PendingOrder(symbol="AAA", side="SELL", quantity=10, price=2.0, order_type="LIMIT",
                        status=PENDING, source="kss", source_ref=f"pyramid:{s.id}:sl"))     # SELL — ignored
    db.add(PendingOrder(symbol="AAA", side="BUY", quantity=10, price=2.0, order_type="LIMIT",
                        status=PENDING, source="kss", source_ref="pyramid:9999:1"))         # other session
    db.commit()
    assert service._pending_wave_notional(db, s.id) == pytest.approx(20.0)


def test_pending_notional_counts_only_unbooked_remainder(db):
    """P1 Fix 1(b): a PARTIALLY-filled wave order contributes only its UNBOOKED remainder — the
    booked slice is already inside total_cost (via handle_fill_event -> on_fill), so counting
    the order's full ORIGINAL quantity here double-charges that slice against the deploy cap
    and can starve the next wave of headroom that is actually free."""
    from app.models import Fill

    # Fix round A / item 2 note: an in-flight partial fill is only reachable while the row
    # stays LINKED to the resting venue order it is filling against (_book_delta only ever
    # touches a linked row; a row only ever becomes UNLINKED again once the cancel/re-place
    # dance has already collapsed its ``quantity`` down to the true remainder — see
    # `test_pending_notional_unlinked_row_is_not_netted_against_lifetime_fills` below for that
    # case). Linking this row is what keeps this test modeling a real reachable state.
    s = _session(db)
    order = PendingOrder(symbol="AAA", side="BUY", quantity=10.0, price=2.0, order_type="LIMIT",
                         status=PENDING, source="kss", source_ref=f"pyramid:{s.id}:1",
                         exchange_order_id="X1")
    db.add(order)
    db.commit()
    db.add(Fill(pending_order_id=order.id, symbol="AAA", side="BUY", quantity=6.0, price=2.0,
               fee=0.0, source_ref=order.source_ref, exchange_order_id="X1"))
    db.commit()
    # 10 booked to 6 -> 4 units still unbooked, at $2 => $8 (NOT the full original $20).
    assert service._pending_wave_notional(db, s.id) == pytest.approx(8.0)


def test_headroom_not_double_counted_after_async_terminal_fill(db, monkeypatch):
    """P1 Fix 1(a): reproduces WLD#13 (audit row id 1532). orders._book_delta must stamp a
    TERMINAL fill's order EXECUTED before it invokes the KSS fill hook — else the hook's own
    headroom check still finds the just-filled order PENDING/APPROVED and double-counts it
    (once as total_cost via on_fill, again as still-pending notional), starving the very next
    wave of headroom that is actually free.

    Cap 240; wave 0 ($40) already filled; wave 1 ($78.40) fills here via the ASYNC reconcile
    path. total_cost after booking = 118.40 -> correct headroom = 121.60 (>= wave 2's $115.248,
    so it queues). The pre-fix bug double-counts wave 1's $78.40 -> headroom 43.20 (< $115.248,
    wave 2 wrongly refused with a `deploy_cap_hit` audit)."""
    from app import execution
    from app.models import APPROVED, WAVE_FILLED, WAVE_SENT, AuditLog, KssWave

    class _StubProvider:
        def pair(self, symbol):
            return f"{symbol}/USDT"

    monkeypatch.setattr(settings, "max_session_deploy_usd", 240.0)
    monkeypatch.setattr(settings, "kss_first_wave_usd", 40.0)
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {})  # no live px -> geometric anchor
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {})       # no spurious TP
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 1e9})

    s = _session(db, entry_price=40.0, distance_pct=2.0, max_waves=4, tp_pct=90.0,
                total_cost=40.0, total_filled_qty=1.0, avg_price=40.0, current_wave=1,
                isolated_fund=100000.0, sl_pct=90.0, first_wave_usd=40.0)
    db.add(KssWave(session_id=s.id, wave_num=0, quantity=1.0, target_price=40.0,
                   status=WAVE_FILLED, filled_qty=1.0, filled_price=40.0))
    order = PendingOrder(symbol="AAA", side="BUY", order_type="LIMIT", quantity=2.0, price=39.2,
                         status=APPROVED, source="kss", source_ref=f"pyramid:{s.id}:wave:1",
                         exchange_order_id="X1", exchange_status="open")
    db.add(order)
    db.commit()
    db.add(KssWave(session_id=s.id, wave_num=1, quantity=order.quantity, target_price=order.price,
                   status=WAVE_SENT, pending_order_id=order.id))
    db.commit()

    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "fetch_live_order",
                        lambda pair, oid: {"status": "closed", "filled": 2.0, "average": 39.2,
                                           "fee": 0.0, "raw_id": "X1"})

    booked = orders.reconcile_live_orders(db)
    assert booked == [order.id]

    pend = db.query(PendingOrder).filter(PendingOrder.status == PENDING,
                                         PendingOrder.source_ref == f"pyramid:{s.id}:wave:2").all()
    assert len(pend) == 1, "wave 2 was NOT queued — the just-filled wave 1 order was double-counted"
    assert db.query(AuditLog).filter(AuditLog.action == "deploy_cap_hit").count() == 0


# ---- enforcement: manual DCA+ (queue_next_wave) ----

def test_manual_wave_rejected_over_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 100.0)
    s = _session(db, total_cost=90.0)                       # headroom 10
    with pytest.raises(ValueError, match="trần triển khai"):
        service.queue_next_wave(db, s.id, amount_usd=50.0)  # $50 > $10 headroom


def test_manual_wave_ok_within_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 1000.0)
    s = _session(db, total_cost=90.0)                       # headroom 910
    res = service.queue_next_wave(db, s.id, amount_usd=50.0)
    assert res["wave_num"] == 1 and res["cost"] > 0


def test_cap_off_allows_large_wave(db):
    s = _session(db, total_cost=90.0)                       # cap 0 = off
    res = service.queue_next_wave(db, s.id, amount_usd=5000.0)
    assert res["wave_num"] == 1


# ---- enforcement: auto-chain (_queue_wave_if_above_sl) ----

def test_auto_chain_skipped_over_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 100.0)
    s = _session(db, total_cost=90.0)
    py = service._to_pyramid(s)
    order = {"symbol": "AAA", "side": "BUY", "quantity": 10.0, "price": 9.0,
             "order_type": "LIMIT", "source": "kss", "source_ref": f"pyramid:{s.id}:1"}
    ok = service._queue_wave_if_above_sl(db, py, s.id, "AAA", order)   # ~$90+ wave > $10 headroom
    assert ok is False
    from app.models import AuditLog
    assert db.query(AuditLog).filter(AuditLog.action == "deploy_cap_hit").count() == 1


# ---- enforcement: pyramid_up base ladder (create_pyramid_up_session) ----
# The base wave is a MARKET buy that never passes _session_deploy_headroom (unlike every later
# rung), so the cap must bound the ladder at its source. GIGGLE #11: scan_fund=1000 → a $486
# base on a $1k book, blowing past max_session_deploy_usd=150 in one shot.

def _stub_exchange_info(monkeypatch):
    monkeypatch.setattr(market, "get_exchange_info",
                        lambda sym: {"stepSize": 0.00001, "minQty": 0.00001})


def test_pyramid_up_ladder_capped_by_deploy_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 150.0)
    monkeypatch.setattr(settings, "scan_fund", 1000.0)
    _stub_exchange_info(monkeypatch)
    row = service.create_pyramid_up_session(
        db, symbol="AAA", entry_price=27.0, tp_pct=4.0, deadline_days=30)
    assert row.isolated_fund <= 150.0


def test_pyramid_up_ladder_uses_scan_fund_when_cap_off(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 0.0)
    monkeypatch.setattr(settings, "scan_fund", 1000.0)
    _stub_exchange_info(monkeypatch)
    row = service.create_pyramid_up_session(
        db, symbol="AAA", entry_price=27.0, tp_pct=4.0, deadline_days=30)
    assert row.isolated_fund > 150.0     # unclamped — sized off scan_fund as before


# ---- enforcement: the Telegram ➕ button (queue_manual_extra_wave) ----

def test_button_rejected_and_rolls_back_over_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 100.0)
    s = _session(db, current_wave=5, max_waves=6, total_cost=95.0)  # full ladder, headroom 5
    # the standard next rung (kss_first_wave_usd sizing) will far exceed $5 → reject + rollback the bump
    monkeypatch.setattr(settings, "kss_first_wave_usd", 1500.0)
    with pytest.raises(ValueError):
        service.queue_manual_extra_wave(db, s.id)
    db.expire_all()
    assert db.get(KssSession, s.id).max_waves == 6                  # bump rolled back


def test_pending_notional_scoped_to_current_venue_order(db):
    """After a cancel-and-re-place, the row's quantity is already the unfilled remainder and its
    exchange_order_id points at the NEW venue order. Fills booked under the OLD venue order must
    not be netted against the remainder — that would under-count resting exposure and overstate
    deploy headroom (the direction that breaches the cap)."""
    from app.models import Fill

    s_ = _session(db)
    order = PendingOrder(symbol="AAA", side="BUY", quantity=6.0, price=2.0, order_type="LIMIT",
                         status=PENDING, source="kss", source_ref=f"pyramid:{s_.id}:1",
                         exchange_order_id="B")
    db.add(order)
    db.commit()
    db.add(Fill(pending_order_id=order.id, symbol="AAA", side="BUY", quantity=4.0, price=2.0,
                fee=0.0, slippage=0.0, realized_pnl=0.0, source_ref=order.source_ref,
                exchange_order_id="A"))  # booked under the PREVIOUS venue order
    db.commit()
    # remainder = the full current quantity (6 x $2), NOT 6-4=2
    assert service._pending_wave_notional(db, s_.id) == pytest.approx(12.0)


def test_pending_notional_unlinked_row_is_not_netted_against_lifetime_fills(db):
    """Fix round A / item 2: an UNLINKED row (exchange_order_id is None — e.g. a failed
    re-place left it PENDING right after `_cancel_resting` already reduced `quantity` to the
    unfilled remainder) must count its FULL current `quantity`, never netted against fills that
    left with the OLD (now-dropped) exchange link. Passing exch_id=None into
    `_booked_qty_fee` sums EVERY fill this row ever had, lifetime, and nets that against the
    already-reduced remainder — under-counting the pending notional (the deploy-cap-breach
    direction)."""
    from app.models import Fill

    s_ = _session(db)
    order = PendingOrder(symbol="AAA", side="BUY", quantity=60.0, price=2.0, order_type="LIMIT",
                         status=PENDING, source="kss", source_ref=f"pyramid:{s_.id}:1",
                         exchange_order_id=None)
    db.add(order)
    db.commit()
    db.add(Fill(pending_order_id=order.id, symbol="AAA", side="BUY", quantity=40.0, price=2.0,
                fee=0.0, slippage=0.0, realized_pnl=0.0, source_ref=order.source_ref,
                exchange_order_id="OLD-DROPPED"))
    db.commit()
    # The full 60 units count — NOT 60-40=20.
    assert service._pending_wave_notional(db, s_.id) == pytest.approx(120.0)


def test_book_delta_stamps_terminal_even_without_new_delta(db):
    """A terminal venue status with nothing NEW to book (fill already recorded) must still stamp
    the row EXECUTED + its exchange_status — otherwise the row stays in every reconcile query
    forever. (The stamp moved around in P1; this pins the no-delta path.)"""
    from app.models import EXECUTED, Fill

    s_ = _session(db)
    order = PendingOrder(symbol="AAA", side="BUY", quantity=5.0, price=2.0, order_type="LIMIT",
                         status=PENDING, source="kss", source_ref=f"pyramid:{s_.id}:1",
                         exchange_order_id="X1")
    db.add(order)
    db.commit()
    db.add(Fill(pending_order_id=order.id, symbol="AAA", side="BUY", quantity=5.0, price=2.0,
                fee=0.0, slippage=0.0, realized_pnl=0.0, source_ref=order.source_ref,
                exchange_order_id="X1"))  # the venue's cumulative fill, already booked
    db.commit()
    res = {"status": "filled", "filled": 5.0, "average": 2.0, "fee": 0.0, "raw_id": "X1"}
    assert orders._book_delta(db, order, res) is False  # delta 0 -> nothing new booked
    assert order.status == EXECUTED
    assert order.exchange_status == "filled"


def test_stamp_is_flushed_and_visible_before_the_kss_hook_runs(db, monkeypatch):
    """Pins P1 Fix 1(a) at the actual mechanism, not just its symptom. Nothing else in the
    suite asserts that `_book_delta` stamps the order EXECUTED and FLUSHES it before invoking
    the KSS fill hook — `test_headroom_not_double_counted_after_async_terminal_fill` above only
    pins the symptom (wave 2 gets queued), and the companion `_pending_wave_notional` fix
    (netting past fills only for a row that is currently LINKED) makes that test pass either
    way the stamp is ordered relative to the hook.

    Spies on the hook itself and, DURING the call, reads the order's status with a RAW SQL
    query on the same connection (bypassing the ORM identity map, which would just echo the
    in-memory attribute regardless of whether a flush ever happened) — so this only goes green
    if the UPDATE actually reached the transaction before the hook runs. It goes red the moment
    the stamp moves back below the hook (or back below the flush)."""
    from sqlalchemy import text

    from app.models import APPROVED, EXECUTED

    s_ = _session(db)
    order = PendingOrder(symbol="AAA", side="BUY", quantity=5.0, price=2.0, order_type="LIMIT",
                         status=APPROVED, source="kss", source_ref=f"pyramid:{s_.id}:wave:1",
                         exchange_order_id="X1", exchange_status="open")
    db.add(order)
    db.commit()

    seen: dict = {}

    def _spy(db_inner, source_ref, filled_qty, filled_price):
        row = db_inner.execute(
            text("SELECT status FROM pending_orders WHERE id = :id"), {"id": order.id},
        ).first()
        seen["status"] = row[0]
        return None

    monkeypatch.setattr("app.kss.service.handle_fill_event", _spy)

    res = {"status": "closed", "filled": 5.0, "average": 2.0, "fee": 0.0, "raw_id": "X1"}
    assert orders._book_delta(db, order, res) is True

    assert seen["status"] == EXECUTED, "the hook ran against a row not yet flushed as EXECUTED"


def test_open_session_default_reserve_carries_slack(db, monkeypatch):
    """P1: when the scanner derives isolated_fund itself, it reserves projection x (1+slack%),
    so a later re-quantized rung that costs a hair more than the exact projection (step/tick
    drift between open and the wave — ONDO#14 died over $0.0078) still fits the fund."""
    from app import scanner

    monkeypatch.setattr(settings, "kss_ladder_reserve_slack_pct", 1.0)
    monkeypatch.setattr(service, "projected_ladder_cost",
                        lambda symbol, entry, distance_pct, max_waves: 100.0)
    sid = scanner._open_session(db, "AAA", 100.0, "auto", distance_pct=1.5, tp_pct=4.0,
                                max_waves=3)
    row = db.get(KssSession, sid)
    assert row.isolated_fund == pytest.approx(101.0)
