"""Fast take-profit loop (docs/plan/live-readiness-plan.md task 1.11).

`app.kss.service.run_fast_exit` re-checks ARMED (`tp_trail_floor > 0`) or already-in-profit
`dca_down` sessions against the EXISTING `_trail_after_tp` on `scheduler.kss_fast_exit_sec`'s
cadence — faster than the 90s `kss_exit_check_sec` guard — but ONLY while the WS price feed is
registered and fresh, and it reads ONLY `market.cached_prices` (the already-warm TTL cache), so
it is structurally incapable of making a REST call. Losing/unarmed sessions, hard SL,
crash-detect and the v1 dynamic channel are all untouched here — the 90s guard still covers
every session exactly as before.

The autouse `_clear_market_state` fixture patches `market.live_provider` to raise on any call,
so any test in this file that accidentally reaches REST fails loudly rather than silently
passing on a lucky cache hit.
"""

from __future__ import annotations

import pytest

from app import market, models, orders, scheduler
from app.config import settings
from app.kss import service
from app.models import REJECTED, KssSession


class _FakeFeed:
    """Minimal stand-in for app.data.ws_feed.BinancePriceFeed's freshness check."""

    def __init__(self, fresh: bool):
        self._fresh = fresh

    def is_fresh(self, max_age: float) -> bool:
        return self._fresh


class _RaisingProvider:
    """Any call here is a bug: the fast-exit path must never reach the network."""

    def get_prices(self, symbols, fresh=False):
        raise AssertionError("run_fast_exit must never call the price provider (REST)")

    def get_exchange_info(self, symbol):
        raise AssertionError("run_fast_exit must never call the price provider (REST)")


@pytest.fixture(autouse=True)
def _clear_market_state(monkeypatch):
    market.clear_cache()
    market.unregister_ws_feed()
    monkeypatch.setattr(market, "live_provider", lambda: _RaisingProvider())
    yield
    market.clear_cache()
    market.unregister_ws_feed()


def _session(db, *, avg=100.0, qty=3.0, tp_pct=5.0, status=models.SESSION_ACTIVE,
             strategy_mode="dca_down", tp_trail_floor=0.0, trail_sl_price=0.0,
             peak_price=0.0) -> KssSession:
    row = KssSession(
        symbol="SOL", entry_price=avg, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=tp_pct, timeout_x_min=60, gap_y_min=5,
        status=status, current_wave=1, avg_price=avg, total_filled_qty=qty,
        total_cost=avg * qty, strategy_mode=strategy_mode, tp_trail_floor=tp_trail_floor,
        trail_sl_price=trail_sl_price, peak_price=peak_price,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --- market.cached_prices / market.ws_feed_fresh — the primitives ----------------------------


def test_cached_prices_never_touches_the_network():
    market.note_ws_prices({"SOL": 12.3})

    prices = market.cached_prices(["SOL", "MISSING"])

    assert prices == {"SOL": 12.3}  # missing symbol simply absent, no fetch attempted


def test_ws_feed_fresh_false_when_no_feed_registered():
    assert market._ws_feed is None
    assert market.ws_feed_fresh() is False


def test_ws_feed_fresh_reflects_the_feed():
    market.register_ws_feed(_FakeFeed(fresh=True))
    assert market.ws_feed_fresh() is True
    market.register_ws_feed(_FakeFeed(fresh=False))
    assert market.ws_feed_fresh() is False


# --- off by default -----------------------------------------------------------------------


def test_off_by_default_fast_exit_should_not_run():
    assert settings.kss_fast_exit_sec == 0.0
    assert scheduler.fast_exit_should_run() is False


def test_knob_on_enables_the_gate(monkeypatch):
    monkeypatch.setattr(settings, "kss_fast_exit_sec", 2.0)
    assert scheduler.fast_exit_should_run() is True


def test_run_fast_exit_with_no_ws_feed_is_a_no_op_and_never_calls_the_provider(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db, avg=100.0)
    market.note_ws_prices({"SOL": 200.0})  # a price IS cached...
    assert market._ws_feed is None  # ...but no feed is registered — must still no-op

    out = service.run_fast_exit(db)

    assert out["evaluated"] == 0
    assert out["armed"] == 0
    assert out["exited"] == 0
    assert out["skipped_reason"]
    db.refresh(row)
    assert row.tp_trail_floor == 0.0


# --- WS stale -------------------------------------------------------------------------------


def test_stale_ws_feed_does_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db, avg=100.0)
    market.register_ws_feed(_FakeFeed(fresh=False))
    market.note_ws_prices({"SOL": 200.0})

    out = service.run_fast_exit(db)

    assert out["skipped_reason"]
    assert out["evaluated"] == 0
    db.refresh(row)
    assert row.tp_trail_floor == 0.0


# --- selection: a losing, unarmed session is left to the 90s guard -------------------------


def test_losing_unarmed_session_is_not_evaluated(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db, avg=100.0)
    market.register_ws_feed(_FakeFeed(fresh=True))
    market.note_ws_prices({"SOL": 90.0})  # below avg, unarmed

    out = service.run_fast_exit(db)

    assert out["evaluated"] == 0
    assert out["skipped_reason"] is None  # the tick itself ran — just nothing qualified
    db.refresh(row)
    assert row.tp_trail_floor == 0.0


def test_a_pyramid_up_session_in_profit_is_not_selected(db, monkeypatch):
    """run_fast_exit is dca_down-only; pyramid_up has its own channel untouched here."""
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db, avg=100.0, strategy_mode="pyramid_up")
    market.register_ws_feed(_FakeFeed(fresh=True))
    market.note_ws_prices({"SOL": 150.0})  # well in profit

    out = service.run_fast_exit(db)

    assert out["evaluated"] == 0
    db.refresh(row)
    assert row.tp_trail_floor == 0.0


# --- arming: in-profit unarmed session reaching target ---------------------------------------


def test_in_profit_unarmed_session_reaching_target_is_armed_and_supersedes_the_resting_tp(
    db, monkeypatch,
):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db, avg=100.0)
    target = service._to_pyramid(row).estimated_tp_price
    tp_row, _ = orders.queue_order(
        db, symbol="SOL", side="SELL", quantity=row.total_filled_qty, price=target,
        order_type="LIMIT", source="kss", source_ref=f"pyramid:{row.id}:tp",
    )
    market.register_ws_feed(_FakeFeed(fresh=True))
    market.note_ws_prices({"SOL": target})

    out = service.run_fast_exit(db)

    assert out["skipped_reason"] is None
    assert out["evaluated"] == 1
    assert out["armed"] == 1
    assert out["exited"] == 0
    db.refresh(row)
    assert row.tp_trail_floor == target
    assert row.trail_active is False  # must never wake the v1 channel
    db.refresh(tp_row)
    assert tp_row.status == REJECTED
    assert tp_row.reject_reason == "resting-tp: superseded by trail-after-tp"


def test_in_profit_but_below_target_is_evaluated_but_not_armed(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db, avg=100.0)
    market.register_ws_feed(_FakeFeed(fresh=True))
    market.note_ws_prices({"SOL": 101.0})  # in profit, but below the TP target

    out = service.run_fast_exit(db)

    assert out["evaluated"] == 1
    assert out["armed"] == 0
    db.refresh(row)
    assert row.tp_trail_floor == 0.0


# --- exit: an armed session whose price falls to the stop is exited AND filled ---------------


def test_armed_session_falling_to_the_stop_is_exited_and_filled_in_the_same_call(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db, avg=100.0, qty=3.0, tp_trail_floor=105.0, trail_sl_price=106.7,
                   peak_price=110.0)
    market.register_ws_feed(_FakeFeed(fresh=True))
    market.note_ws_prices({"SOL": 50.0})  # crash far below the ratcheted stop

    out = service.run_fast_exit(db)

    assert out["evaluated"] == 1
    assert out["exited"] == 1
    db.refresh(row)
    assert row.status == models.SESSION_COMPLETED  # queued AND force-filled in this same call


# --- run_position_guard: byte-identical after the force-fill refactor ------------------------


def test_run_position_guard_output_unchanged_after_the_force_fill_refactor(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_after_tp_pct", 3.0)
    row = _session(db, avg=100.0)
    target = service._to_pyramid(row).estimated_tp_price
    monkeypatch.setattr("app.market.get_current_prices",
                        lambda syms, force=False: dict.fromkeys(syms, target * 0.5))
    monkeypatch.setattr("app.orders.get_current_prices",
                        lambda syms: dict.fromkeys(syms, target * 0.5))
    row.tp_trail_floor = target
    row.trail_sl_price = target
    row.peak_price = target
    db.commit()

    out = service.run_position_guard(db)

    assert set(out) == {"checked", "exited"}
    assert out["checked"] == 1
    assert out["exited"] == [row.id]
    db.refresh(row)
    assert row.status == models.SESSION_COMPLETED  # the force-fill helper still runs and fills


# --- scheduler: lock held skips the tick with no DB writes ------------------------------------


def test_lock_held_skips_the_tick_with_no_writes(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(service, "run_fast_exit", lambda db: calls.append(1) or {})
    assert scheduler._work_lock.acquire(blocking=False)
    try:
        scheduler._fast_exit_once()  # must return immediately without opening a DB session
    finally:
        scheduler._work_lock.release()

    assert calls == []


def test_lock_free_runs_and_releases_the_lock(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(service, "run_fast_exit", lambda db: calls.append(1) or {})

    scheduler._fast_exit_once()

    assert calls == [1]
    assert scheduler._work_lock.acquire(blocking=False), "the lock must be released afterward"
    scheduler._work_lock.release()


# --- item 5: the live stop replace budget still bounds a tight loop --------------------------


class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


class _StopVenue:
    def __init__(self):
        self.placed: list[float] = []
        self._n = 0

    def place_stop(self, pair, quantity, stop_price, limit_price, client_order_id=None):
        self._n += 1
        self.placed.append(stop_price)
        return {"raw_id": f"S{self._n}", "status": "open", "price": 0.0,
                "quantity": quantity, "fee": 0.0}

    def cancel(self, pair, order_id):
        return None

    def fetch(self, pair, order_id):
        # The old stop is cancelled and re-fetched (never filled) on every replace.
        return {"status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0,
                "raw_id": order_id}


def test_maintain_live_stop_ratchet_step_bounds_replaces_under_a_tight_loop(db, monkeypatch):
    """A fast-exit loop at 1-2s calls `_maintain_live_stop` on every ratchet tick — tens of
    times more often than the 90s guard. The ratchet-step gate must bound ACTUAL venue
    placements to how much the stop genuinely moved, not to how often this was called: Binance
    does not refund the unfilled-order count on cancel, so an unbounded replace rate driven by
    call frequency alone would be an account-level risk."""
    from app import execution

    venue = _StopVenue()
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "place_live_stop_order", venue.place_stop)
    monkeypatch.setattr(execution, "cancel_live_order", venue.cancel)
    monkeypatch.setattr(execution, "fetch_live_order", venue.fetch)
    monkeypatch.setattr(execution, "rate_hold_active", lambda: False)
    monkeypatch.setattr(execution, "assert_order_budget_available", lambda urgent=False: None)
    monkeypatch.setattr(
        "app.kss.pyramid.get_exchange_info",
        lambda s: {"minQty": 0.001, "stepSize": 0.001, "minNotional": 5.0},
    )
    settings.kss_live_stop_orders = True
    settings.live_trading = True
    settings.kss_stop_ratchet_step_pct = 0.5
    settings.kss_stop_limit_slip_pct = 0.3
    settings.kss_stop_max_replaces = 40

    row = _session(db, avg=100.0, trail_sl_price=95.0)

    # 100 ticks, each a 0.001% ratchet — cumulative ~0.1% over all of them, still below the
    # 0.5% step — simulating a tight fast-exit loop tracking a slowly-drifting peak.
    for _ in range(100):
        row.trail_sl_price *= 1.00001
        db.commit()
        service._maintain_live_stop(db, row, row.trail_sl_price)

    assert len(venue.placed) == 1, "100 sub-step ticks must place the stop only once (first placement)"

    row.trail_sl_price *= 1.01  # a real move that clears the 0.5% ratchet step
    db.commit()
    service._maintain_live_stop(db, row, row.trail_sl_price)

    assert len(venue.placed) == 2, "only a genuine >=step move may trigger a second placement"

    # And the inverse: enough tiny ticks to cumulatively clear the step DOES bound replaces to
    # how many times the step was actually crossed, not to the 300 calls made.
    before = len(venue.placed)
    for _ in range(300):
        row.trail_sl_price *= 1.0005  # ~0.05%/tick; crosses 0.5% roughly every 10 ticks
        db.commit()
        service._maintain_live_stop(db, row, row.trail_sl_price)
    total_move_pct = (row.trail_sl_price / venue.placed[before - 1] - 1) * 100.0
    expected_replaces_upper_bound = int(total_move_pct / settings.kss_stop_ratchet_step_pct) + 1
    assert len(venue.placed) - before <= expected_replaces_upper_bound, (
        "replace count must track the price move, not the 300 calls made"
    )
