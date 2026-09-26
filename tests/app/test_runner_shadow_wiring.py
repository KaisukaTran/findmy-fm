"""DB glue, scheduler wiring, config knob and API for the runner-shadow SHADOW feature
(app.kss.runner_shadow). Pure math lives in test_runner_shadow.py; this file only checks that the
plumbing around it — sync/tick/summary, the scheduler tick, the knob, and the read API — behaves,
and above all that it can never affect a real exit.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import market, models, runtime, scheduler
from app.clock import utcnow
from app.config import settings
from app.kss import runner_shadow as rs
from app.kss import service
from app.main import app
from app.models import Fill, KssSession, PendingOrder


class _FakeFeed:
    def __init__(self, fresh: bool):
        self._fresh = fresh

    def is_fresh(self, max_age: float) -> bool:
        return self._fresh


@pytest.fixture(autouse=True)
def _clear_market(monkeypatch):
    market.clear_cache()
    market.unregister_ws_feed()
    yield
    market.clear_cache()
    market.unregister_ws_feed()


def _session(db, *, symbol="SOL", avg=100.0, deadline_at=None) -> KssSession:
    row = KssSession(
        symbol=symbol, entry_price=avg, distance_pct=2.0, max_waves=5, isolated_fund=1000.0,
        tp_pct=5.0, timeout_x_min=60, gap_y_min=5, status=models.SESSION_COMPLETED,
        current_wave=1, avg_price=avg, total_filled_qty=10.0, total_cost=avg * 10.0,
        deadline_at=deadline_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _tp_fill(db, session: KssSession, *, price=110.0, qty=10.0, realized_pnl=50.0,
             executed_at=None) -> Fill:
    order = PendingOrder(
        symbol=session.symbol, side="SELL", order_type="MARKET", quantity=qty, price=price,
        source="kss", source_ref=f"pyramid:{session.id}:tp", status="executed",
    )
    db.add(order)
    db.commit()
    fill = Fill(
        pending_order_id=order.id, symbol=session.symbol, side="SELL", quantity=qty, price=price,
        realized_pnl=realized_pnl, source_ref=f"pyramid:{session.id}:tp",
        executed_at=executed_at or utcnow(),
    )
    db.add(fill)
    db.commit()
    db.refresh(fill)
    return fill


# --------------------------------------------------------------------------- sync_new_tp_fills


def test_first_call_ever_sets_the_watermark_and_creates_nothing(db):
    session = _session(db)
    _tp_fill(db, session)  # a TP fill that existed BEFORE the feature ever ran

    created = rs.sync_new_tp_fills(db)

    assert created == 0
    assert db.query(models.RunnerShadow).count() == 0
    assert runtime.get(db, rs.WATERMARK_KEY) is not None


def test_a_tp_fill_after_the_watermark_gets_six_shadow_rows(db):
    session = _session(db)
    rs.sync_new_tp_fills(db)  # sets the watermark, nothing to backfill

    fill = _tp_fill(db, session, price=110.0, qty=10.0, realized_pnl=50.0,
                     executed_at=utcnow() + timedelta(seconds=1))
    created = rs.sync_new_tp_fills(db)

    assert created == 6
    rows = db.query(models.RunnerShadow).filter(models.RunnerShadow.fill_id == fill.id).all()
    assert len(rows) == 6
    variants_gaps = {(r.variant, r.gap_pct) for r in rows}
    assert variants_gaps == {(v, g) for v in ("v4a", "v5") for g in rs.GAPS}
    for r in rows:
        assert r.session_id == session.id
        assert r.symbol == session.symbol
        assert r.avg_price == session.avg_price
        assert r.tp_price == 110.0
        assert r.v0_net == 50.0
        assert r.state == rs.STATE_WATCH


def test_sync_is_idempotent(db):
    session = _session(db)
    rs.sync_new_tp_fills(db)
    _tp_fill(db, session, executed_at=utcnow() + timedelta(seconds=1))

    first = rs.sync_new_tp_fills(db)
    second = rs.sync_new_tp_fills(db)

    assert first == 6
    assert second == 0
    assert db.query(models.RunnerShadow).count() == 6


def test_a_buy_fill_is_never_picked_up(db):
    session = _session(db)
    rs.sync_new_tp_fills(db)
    order = PendingOrder(symbol=session.symbol, side="BUY", order_type="MARKET", quantity=1.0,
                          price=100.0, source="kss", source_ref=f"pyramid:{session.id}:wave:0",
                          status="executed")
    db.add(order)
    db.commit()
    db.add(Fill(pending_order_id=order.id, symbol=session.symbol, side="BUY", quantity=1.0,
                price=100.0, realized_pnl=0.0, source_ref=f"pyramid:{session.id}:wave:0",
                executed_at=utcnow() + timedelta(seconds=1)))
    db.commit()

    created = rs.sync_new_tp_fills(db)

    assert created == 0


def test_a_fill_whose_session_is_gone_is_skipped_not_crashed(db):
    session = _session(db)
    rs.sync_new_tp_fills(db)
    fill = _tp_fill(db, session, executed_at=utcnow() + timedelta(seconds=1))
    db.delete(session)
    db.commit()

    created = rs.sync_new_tp_fills(db)

    assert created == 0
    assert db.query(models.RunnerShadow).filter(models.RunnerShadow.fill_id == fill.id).count() == 0


def test_deadline_falls_back_to_60_days_when_the_session_has_none(db):
    session = _session(db, deadline_at=None)
    rs.sync_new_tp_fills(db)
    now = utcnow() + timedelta(seconds=1)
    fill = _tp_fill(db, session, executed_at=now)

    rs.sync_new_tp_fills(db)

    row = db.query(models.RunnerShadow).filter(models.RunnerShadow.fill_id == fill.id).first()
    assert row.deadline_at == pytest.approx(now + timedelta(days=60), abs=timedelta(seconds=1))


# --------------------------------------------------------------------------- tick


def test_tick_is_a_no_op_when_the_ws_feed_is_not_fresh(db):
    session = _session(db)
    rs.sync_new_tp_fills(db)
    fill = _tp_fill(db, session, executed_at=utcnow() + timedelta(seconds=1))
    rs.sync_new_tp_fills(db)
    market.register_ws_feed(_FakeFeed(fresh=False))

    updated = rs.tick(db, {session.symbol: 200.0}, utcnow())

    assert updated == 0
    row = db.query(models.RunnerShadow).filter(models.RunnerShadow.fill_id == fill.id).first()
    assert row.state == rs.STATE_WATCH
    assert row.last_seen_at is None  # never touched


def test_tick_advances_open_rows_when_the_feed_is_fresh(db):
    session = _session(db)
    rs.sync_new_tp_fills(db)
    fill = _tp_fill(db, session, price=110.0, qty=10.0, realized_pnl=50.0,
                     executed_at=utcnow() + timedelta(seconds=1))
    rs.sync_new_tp_fills(db)
    market.register_ws_feed(_FakeFeed(fresh=True))

    updated = rs.tick(db, {session.symbol: 130.0}, utcnow() + timedelta(minutes=1))

    assert updated == 6
    rows = db.query(models.RunnerShadow).filter(models.RunnerShadow.fill_id == fill.id).all()
    assert all(r.last_price == 130.0 for r in rows)


def test_tick_skips_a_row_whose_symbol_has_no_price(db):
    session = _session(db)
    rs.sync_new_tp_fills(db)
    _tp_fill(db, session, executed_at=utcnow() + timedelta(seconds=1))
    rs.sync_new_tp_fills(db)
    market.register_ws_feed(_FakeFeed(fresh=True))

    updated = rs.tick(db, {}, utcnow() + timedelta(minutes=1))

    assert updated == 0


def test_open_symbols_reflects_only_open_rows(db):
    session = _session(db)
    rs.sync_new_tp_fills(db)
    _tp_fill(db, session, executed_at=utcnow() + timedelta(seconds=1))
    rs.sync_new_tp_fills(db)

    assert rs.open_symbols(db) == [session.symbol]

    market.register_ws_feed(_FakeFeed(fresh=True))
    for row in db.query(models.RunnerShadow).all():
        row.state = rs.STATE_CLOSED
    db.commit()

    assert rs.open_symbols(db) == []


# --------------------------------------------------------------------------- summary


def test_summary_shape_has_all_six_buckets(db):
    out = rs.summary(db)
    keys = {(b["variant"], b["gap_pct"]) for b in out["buckets"]}
    assert keys == {(v, g) for v in ("v4a", "v5") for g in rs.GAPS}
    assert out["recent"] == []


def test_summary_counts_closed_open_and_skipped_and_worst_best(db):
    session = _session(db)
    rs.sync_new_tp_fills(db)
    _tp_fill(db, session, price=110.0, qty=10.0, realized_pnl=50.0,
             executed_at=utcnow() + timedelta(seconds=1))
    rs.sync_new_tp_fills(db)
    market.register_ws_feed(_FakeFeed(fresh=True))
    now = utcnow() + timedelta(minutes=1)

    v4a2 = db.query(models.RunnerShadow).filter(
        models.RunnerShadow.variant == "v4a", models.RunnerShadow.gap_pct == 2.0).one()
    rs.tick(db, {session.symbol: v4a2.stop}, now)  # trigger the v4a/2% row closed
    db.refresh(v4a2)
    assert v4a2.state == rs.STATE_CLOSED

    out = rs.summary(db)
    bucket = next(b for b in out["buckets"] if b["variant"] == "v4a" and b["gap_pct"] == 2.0)
    assert bucket["n_closed"] == 1
    assert bucket["n_open"] == 0
    assert bucket["worst_usd"] == bucket["best_usd"] == pytest.approx(v4a2.diff_usd)

    other_bucket = next(b for b in out["buckets"] if b["variant"] == "v4a" and b["gap_pct"] == 3.0)
    assert other_bucket["n_open"] == 1
    assert other_bucket["n_closed"] == 0


def test_summary_recent_open_rows_carry_their_mark_to_market_estimate(db):
    """The recent-rows table labels "Chênh $" as the estimate for an OPEN row — the stored
    diff_usd/net_usd are 0 until close, so the summary must hand the template the estimate."""
    session = _session(db)
    rs.sync_new_tp_fills(db)
    _tp_fill(db, session, price=110.0, qty=10.0, realized_pnl=50.0,
             executed_at=utcnow() + timedelta(seconds=1))
    rs.sync_new_tp_fills(db)
    market.register_ws_feed(_FakeFeed(fresh=True))
    rs.tick(db, {session.symbol: 120.0}, utcnow() + timedelta(minutes=1))

    out = rs.summary(db)

    v4a = next(r for r in out["recent"] if r["variant"] == "v4a" and r["gap_pct"] == 5.0)
    assert v4a["state"] == rs.STATE_WATCH
    row = db.get(models.RunnerShadow, v4a["id"])
    expected = rs.mark_to_market(rs._state_from_row(row), rs._shadow_params())
    assert expected > 0
    assert v4a["diff_est_usd"] == pytest.approx(expected)
    assert v4a["net_est_usd"] == pytest.approx(50.0 + expected)


def test_summary_recent_closed_rows_estimate_equals_the_realized_diff(db):
    session = _session(db)
    rs.sync_new_tp_fills(db)
    _tp_fill(db, session, price=110.0, qty=10.0, realized_pnl=50.0,
             executed_at=utcnow() + timedelta(seconds=1))
    rs.sync_new_tp_fills(db)
    market.register_ws_feed(_FakeFeed(fresh=True))
    rs.tick(db, {session.symbol: 50.0}, utcnow() + timedelta(minutes=1))  # every v4a stops out

    out = rs.summary(db)

    for r in (r for r in out["recent"] if r["variant"] == "v4a"):
        assert r["state"] == rs.STATE_CLOSED
        assert r["diff_est_usd"] == pytest.approx(r["diff_usd"])
        assert r["net_est_usd"] == pytest.approx(r["net_usd"])


def test_summary_recent_rows_are_capped_and_newest_first(db):
    session = _session(db)
    rs.sync_new_tp_fills(db)
    _tp_fill(db, session, executed_at=utcnow() + timedelta(seconds=1))
    rs.sync_new_tp_fills(db)

    out = rs.summary(db, recent_limit=2)

    assert len(out["recent"]) == 2
    ids = [r["id"] for r in out["recent"]]
    assert ids == sorted(ids, reverse=True)


# --------------------------------------------------------------------------- config knob


def test_runner_shadow_enabled_defaults_true():
    assert settings.runner_shadow_enabled is True


def test_runner_shadow_slip_pct_default():
    assert settings.runner_shadow_slip_pct == pytest.approx(0.1)


def test_kss_settings_round_trip(db, monkeypatch):
    monkeypatch.setattr(settings, "runner_shadow_enabled", True)
    out = runtime.set_kss_settings(db, {"runner_shadow_enabled": False, "runner_shadow_slip_pct": 0.25})
    assert out["runner_shadow_enabled"] is False
    assert out["runner_shadow_slip_pct"] == pytest.approx(0.25)
    assert settings.runner_shadow_enabled is False
    assert runtime.get(db, "kss:runner_shadow_enabled") == "False"


def test_sync_from_db_restores_the_knob(db, monkeypatch):
    monkeypatch.setattr(settings, "runner_shadow_enabled", True)
    runtime.set(db, "kss:runner_shadow_enabled", "False")
    runtime.sync_from_db(db)
    assert settings.runner_shadow_enabled is False


# --------------------------------------------------------------------------- scheduler wiring


def test_fast_exit_pass_runs_runner_shadow_after_run_fast_exit(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(service, "run_fast_exit", lambda db: order.append("exit") or {})
    monkeypatch.setattr(scheduler, "_runner_shadow_once", lambda db: order.append("shadow"))

    scheduler._fast_exit_pass()

    assert order == ["exit", "shadow"]


def test_fast_exit_pass_swallows_a_runner_shadow_exception(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(service, "run_fast_exit", lambda db: order.append("exit") or {})

    def _boom(db):
        order.append("shadow")
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler, "_runner_shadow_once", _boom)

    scheduler._fast_exit_pass()  # must not raise

    assert order == ["exit", "shadow"]


def test_fast_exit_once_itself_never_runs_the_shadow(monkeypatch):
    """The exit tick is exit-only: the shadow is not inside `_fast_exit_once` at all."""
    calls: list[str] = []
    monkeypatch.setattr(service, "run_fast_exit", lambda db: calls.append("exit") or {})
    monkeypatch.setattr(scheduler, "_runner_shadow_once", lambda db: calls.append("shadow"))

    scheduler._fast_exit_once()

    assert calls == ["exit"]


def test_runner_shadow_runs_outside_work_lock(monkeypatch):
    """`_work_lock` is acquired BLOCKING by the 90s guard and the 30-min cycle — any time the
    shadow spent under it would delay them directly. It must run with the lock released."""
    seen: list[bool] = []
    monkeypatch.setattr(service, "run_fast_exit", lambda db: {})
    monkeypatch.setattr(scheduler, "_runner_shadow_once",
                        lambda db: seen.append(scheduler._work_lock.locked()))

    scheduler._fast_exit_pass()

    assert seen == [False]


def test_runner_shadow_uses_its_own_db_session(monkeypatch):
    sessions: dict[str, object] = {}
    monkeypatch.setattr(service, "run_fast_exit", lambda db: sessions.setdefault("exit", db) and {})
    monkeypatch.setattr(scheduler, "_runner_shadow_once", lambda db: sessions.setdefault("shadow", db))

    scheduler._fast_exit_pass()

    assert sessions["exit"] is not sessions["shadow"]


def test_shadow_commit_can_never_persist_uncommitted_exit_state(db, monkeypatch):
    """If the exit path leaves work UNCOMMITTED (e.g. an approve_order that raised mid-way inside
    `_force_fill_queued_exits`), closing its session discards it — exactly as before the shadow
    existed. A shadow commit on that same session would instead persist the half-done exit."""
    monkeypatch.setattr(settings, "runner_shadow_enabled", True)
    market.register_ws_feed(_FakeFeed(fresh=True))

    def _exit_leaves_dirty_state(exit_db):
        exit_db.add(PendingOrder(symbol="HALF", side="SELL", order_type="MARKET", quantity=1.0,
                                 price=1.0, source="kss", source_ref="pyramid:999:sl",
                                 status="pending"))
        return {}

    monkeypatch.setattr(service, "run_fast_exit", _exit_leaves_dirty_state)

    scheduler._fast_exit_pass()  # the real shadow runs: its first call commits the watermark

    assert runtime.get(db, rs.WATERMARK_KEY) is not None  # the shadow really did commit
    assert db.query(PendingOrder).filter(PendingOrder.symbol == "HALF").count() == 0


def test_runner_shadow_once_is_gated_on_the_knob(db, monkeypatch):
    monkeypatch.setattr(settings, "runner_shadow_enabled", False)
    calls: list[str] = []
    monkeypatch.setattr(rs, "sync_new_tp_fills", lambda db: calls.append("sync") or 0)

    scheduler._runner_shadow_once(db)

    assert calls == []


def test_turning_the_knob_off_then_on_never_backfills_the_off_period(db, monkeypatch):
    """A TP fill that happens while the shadow is OFF was never observed live — re-enabling
    must re-seed the watermark to "now", not build rows from that unobserved history."""
    session = _session(db)
    monkeypatch.setattr(settings, "runner_shadow_enabled", True)
    scheduler._runner_shadow_once(db)  # seeds the watermark
    assert runtime.get(db, rs.WATERMARK_KEY) is not None

    monkeypatch.setattr(settings, "runner_shadow_enabled", False)
    scheduler._runner_shadow_once(db)
    _tp_fill(db, session, executed_at=utcnow() + timedelta(seconds=1))  # happens while OFF

    monkeypatch.setattr(settings, "runner_shadow_enabled", True)
    scheduler._runner_shadow_once(db)

    assert db.query(models.RunnerShadow).count() == 0


def test_a_restart_with_the_knob_on_keeps_the_watermark(db, monkeypatch):
    """A process restart is not "off": the stored watermark survives, so the first TP after a
    restart IS tracked and nothing gets re-seeded past it."""
    session = _session(db)
    monkeypatch.setattr(settings, "runner_shadow_enabled", True)
    scheduler._runner_shadow_once(db)
    before = runtime.get(db, rs.WATERMARK_KEY)
    fill = _tp_fill(db, session, executed_at=utcnow() + timedelta(seconds=1))

    scheduler._runner_shadow_once(db)  # "after restart": same DB, fresh process state

    assert runtime.get(db, rs.WATERMARK_KEY) == before
    assert db.query(models.RunnerShadow).filter(models.RunnerShadow.fill_id == fill.id).count() == 6


def test_a_ws_price_for_a_symbol_with_no_open_session_reaches_the_shadow(db, monkeypatch):
    """After its TP the session is COMPLETED, so nothing else watches the coin. The WS feed's
    `note_ws_prices` has no watched-symbol filter (`!miniTicker@arr` keeps every quote pair),
    so its price must still land in `cached_prices` and advance the shadow rows."""
    monkeypatch.setattr(settings, "runner_shadow_enabled", True)
    session = _session(db, symbol="ZZZ")  # status COMPLETED — no open session anywhere
    scheduler._runner_shadow_once(db)
    _tp_fill(db, session, price=110.0, executed_at=utcnow() + timedelta(seconds=1))
    market.register_ws_feed(_FakeFeed(fresh=True))
    market.note_ws_prices({"ZZZ": 111.0, "OTHER": 5.0})  # exactly what the feed callback does

    scheduler._runner_shadow_once(db)

    rows = db.query(models.RunnerShadow).all()
    assert len(rows) == 6
    assert all(r.last_price == 111.0 for r in rows)


def test_runner_shadow_once_runs_sync_and_tick_in_order(db, monkeypatch):
    monkeypatch.setattr(settings, "runner_shadow_enabled", True)
    calls: list[str] = []
    monkeypatch.setattr(rs, "sync_new_tp_fills", lambda db: calls.append("sync") or 0)
    monkeypatch.setattr(rs, "open_symbols", lambda db: ["SOL"])
    monkeypatch.setattr(rs, "tick", lambda db, prices, now: calls.append("tick") or 0)
    monkeypatch.setattr(market, "cached_prices", lambda syms: {})

    scheduler._runner_shadow_once(db)

    assert calls == ["sync", "tick"]


# --------------------------------------------------------------------------- API


def test_api_runner_shadow_returns_summary_shape(db):
    client = TestClient(app)
    resp = client.get("/api/kss/runner-shadow")
    assert resp.status_code == 200
    body = resp.json()
    assert "buckets" in body and "recent" in body
    assert len(body["buckets"]) == 6


def test_partial_runner_shadow_renders_with_and_without_rows(db):
    client = TestClient(app)
    empty = client.get("/partials/runner-shadow")
    assert empty.status_code == 200
    assert "Theo dõi thả nổi" in empty.text

    session = _session(db)
    rs.sync_new_tp_fills(db)
    _tp_fill(db, session, executed_at=utcnow() + timedelta(seconds=1))
    rs.sync_new_tp_fills(db)

    filled = client.get("/partials/runner-shadow")
    assert filled.status_code == 200
    assert session.symbol in filled.text
