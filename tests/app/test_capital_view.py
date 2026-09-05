"""
Capital-utilisation panel (`portfolio.capital_view` / `portfolio.capital_yield_view`).

Reuses `summary_view` / `risk.account_equity` / `scanner._session_lock` instead of
re-deriving equity, cash or the lend-the-idle-reservation rule.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import models, portfolio
from app.clock import utcnow
from app.config import settings
from app.main import app as fastapi_app
from app.models import (
    PENDING,
    SESSION_ACTIVE,
    SESSION_COMPLETED,
    SESSION_STOPPED,
    Fill,
    KssSession,
    PendingOrder,
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    with TestClient(fastapi_app) as c:
        yield c


def _position(db, symbol, quantity, avg_entry_price, total_cost):
    db.add(models.Position(
        symbol=symbol, quantity=quantity, avg_entry_price=avg_entry_price, total_cost=total_cost,
    ))


def _active_session(db, symbol, isolated_fund, total_cost):
    s = KssSession(
        symbol=symbol, entry_price=100.0, distance_pct=2.0, max_waves=5,
        isolated_fund=isolated_fund, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
        status=SESSION_ACTIVE, total_cost=total_cost, started_at=utcnow(), created_at=utcnow(),
    )
    db.add(s)
    db.commit()
    return s


def _resting_buy(db, symbol, quantity, price, order_type="LIMIT", side="BUY", status=PENDING):
    o = PendingOrder(
        symbol=symbol, side=side, order_type=order_type, quantity=quantity, price=price,
        source="manual", status=status,
    )
    db.add(o)
    db.commit()
    return o


def test_capital_view_decomposition_arithmetic(db, monkeypatch):
    monkeypatch.setattr(settings, "account_equity", 2000.0)
    monkeypatch.setattr(settings, "equity_backup_pct", 25.0)
    monkeypatch.setattr(settings, "max_concurrent_sessions", 10)
    monkeypatch.setattr(settings, "scan_fund", 1000.0)
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))

    # Two active sessions: one under the 50%-filled lend threshold (locks only its
    # deployed cash), one over it (locks its full reservation).
    _active_session(db, "AAA", isolated_fund=600.0, total_cost=200.0)   # used 200 < 300 -> lock 200
    _active_session(db, "BBB", isolated_fund=400.0, total_cost=350.0)   # used 350 >= 200 -> lock 400

    # Matching open positions so `summary_view` cash reflects the deployed cost (price ==
    # avg -> zero unrealized, keeps equity == account_equity exactly).
    _position(db, "AAA", quantity=2.0, avg_entry_price=100.0, total_cost=200.0)
    _position(db, "BBB", quantity=3.5, avg_entry_price=100.0, total_cost=350.0)
    db.commit()

    # Resting BUY limit orders count toward resting_buy; a SELL, a MARKET BUY and an
    # already-approved BUY must all be excluded.
    _resting_buy(db, "CCC", quantity=10.0, price=5.0)   # 50
    _resting_buy(db, "DDD", quantity=4.0, price=8.0)    # 32
    _resting_buy(db, "AAA", quantity=1.0, price=10.0, side="SELL")            # excluded
    _resting_buy(db, "BBB", quantity=1.0, price=10.0, order_type="MARKET")    # excluded
    _resting_buy(db, "EEE", quantity=1.0, price=10.0, status="approved")      # excluded

    v = portfolio.capital_view(db)

    assert v["equity"] == pytest.approx(2000.0)
    assert v["backup"] == pytest.approx(500.0)
    assert v["budget"] == pytest.approx(1500.0)
    assert v["deployed"] == pytest.approx(550.0)
    assert v["resting_buy"] == pytest.approx(82.0)
    assert v["committed"] == pytest.approx(1000.0)
    assert v["promised"] == pytest.approx(368.0)
    assert v["free_cash"] == pytest.approx(1368.0)
    assert v["free_after_backup"] == pytest.approx(868.0)  # free_cash(1368) - backup(500)
    assert v["locked_book"] == pytest.approx(600.0)
    assert v["budget_free"] == pytest.approx(900.0)
    assert v["working_pct"] == pytest.approx(31.6)
    assert v["committed_pct"] == pytest.approx(50.0)
    assert v["sessions_active"] == 2
    assert v["sessions_cap"] == 10
    # D2: the threshold is the book's own typical per-session need (committed / sessions),
    # not the flat `scan_fund` constant -- 1000/2=500 here, and budget_free(900) clears it.
    # The OLD constant (scan_fund=1000) would have wrongly flagged "budget" (900 < 1000).
    assert v["typical_need"] == pytest.approx(500.0)
    assert v["binding"] == "none"


def test_capital_view_promised_floors_at_zero(db, monkeypatch):
    """A session that has already deployed + rested more than its reservation must not
    produce a negative `promised`."""
    monkeypatch.setattr(settings, "account_equity", 1000.0)
    _active_session(db, "AAA", isolated_fund=100.0, total_cost=90.0)
    _position(db, "AAA", quantity=0.9, avg_entry_price=100.0, total_cost=90.0)
    db.commit()
    _resting_buy(db, "AAA", quantity=5.0, price=10.0)  # 50 -> deployed+resting > isolated_fund

    v = portfolio.capital_view(db)
    assert v["promised"] == 0.0


def test_binding_is_count_when_at_session_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_sessions", 2)
    _active_session(db, "AAA", isolated_fund=10.0, total_cost=10.0)
    _active_session(db, "BBB", isolated_fund=10.0, total_cost=10.0)

    v = portfolio.capital_view(db)
    assert v["sessions_active"] == 2
    assert v["binding"] == "count"


def test_binding_is_budget_when_under_cap_but_thin_on_cash(db, monkeypatch):
    """D2: `binding` must key off the book's own typical per-session need, not the flat
    `scan_fund` constant. `scan_fund` is set to 0 here specifically so the OLD comparison
    (budget_free < scan_fund -> 0 < 0 -> False -> "none") would have gotten this WRONG; the
    corrected comparison (budget_free < typical_need -> 0 < 150 -> True) still catches it."""
    monkeypatch.setattr(settings, "account_equity", 200.0)
    monkeypatch.setattr(settings, "equity_backup_pct", 25.0)
    monkeypatch.setattr(settings, "max_concurrent_sessions", 10)
    monkeypatch.setattr(settings, "scan_fund", 0.0)
    _active_session(db, "AAA", isolated_fund=150.0, total_cost=150.0)
    _position(db, "AAA", quantity=1.5, avg_entry_price=100.0, total_cost=150.0)
    db.commit()

    v = portfolio.capital_view(db)
    assert v["sessions_active"] < v["sessions_cap"]
    assert v["budget_free"] == pytest.approx(0.0)
    assert v["typical_need"] == pytest.approx(150.0)
    assert v["binding"] == "budget"


def test_binding_is_none_not_budget_against_corrected_threshold(db, monkeypatch):
    """D2 sibling: the OLD flat `scan_fund` constant (~4.3x too large per the audit) would
    wrongly report "budget" here (budget_free 700 < scan_fund 1000); the corrected
    threshold compares against the book's own typical per-session need (230) and correctly
    reports "none" -- there is real room for another typically-sized session."""
    monkeypatch.setattr(settings, "account_equity", 1000.0)
    monkeypatch.setattr(settings, "equity_backup_pct", 0.0)
    monkeypatch.setattr(settings, "max_concurrent_sessions", 10)
    monkeypatch.setattr(settings, "scan_fund", 1000.0)
    _active_session(db, "AAA", isolated_fund=230.0, total_cost=100.0)
    _active_session(db, "BBB", isolated_fund=230.0, total_cost=100.0)
    _active_session(db, "CCC", isolated_fund=230.0, total_cost=100.0)

    v = portfolio.capital_view(db)
    assert v["committed"] == pytest.approx(690.0)
    assert v["locked_book"] == pytest.approx(300.0)
    assert v["budget_free"] == pytest.approx(700.0)
    assert v["typical_need"] == pytest.approx(230.0)
    assert v["binding"] == "none"


def test_binding_is_none_when_capacity_and_budget_are_both_free(db, monkeypatch):
    monkeypatch.setattr(settings, "account_equity", 10000.0)
    monkeypatch.setattr(settings, "equity_backup_pct", 25.0)
    monkeypatch.setattr(settings, "max_concurrent_sessions", 10)
    monkeypatch.setattr(settings, "scan_fund", 1000.0)

    v = portfolio.capital_view(db)
    assert v["sessions_active"] == 0
    assert v["binding"] == "none"


def test_yield_divide_by_zero_guard_on_empty_book(db):
    y = portfolio.capital_yield_view(db)
    assert y["locked_dollar_days"] == 0.0
    assert y["pct_per_locked_dollar_day"] is None


def test_yield_uses_exit_fill_time_not_stale_last_fill_at(db):
    """`last_fill_at` is never touched by an exit fill -- using it under-counts locked
    dollar-days by roughly the gap between the last DCA buy and the actual exit."""
    now = utcnow()
    started = now - timedelta(days=10)
    stale_last_fill_at = now - timedelta(days=6)   # e.g. the last DCA buy
    exit_time = now - timedelta(days=1)             # the real TP sell fill

    s = KssSession(
        symbol="AAA", entry_price=100.0, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
        status=SESSION_COMPLETED, total_cost=400.0, started_at=started, created_at=started,
        last_fill_at=stale_last_fill_at,
    )
    db.add(s)
    db.commit()

    sell = PendingOrder(
        symbol="AAA", side="SELL", order_type="MARKET", quantity=4.0, price=100.0,
        source="kss", source_ref=f"pyramid:{s.id}:tp", status="executed",
    )
    db.add(sell)
    db.commit()
    db.add(Fill(
        pending_order_id=sell.id, symbol="AAA", side="SELL", quantity=4.0, price=100.0,
        realized_pnl=50.0, source_ref=sell.source_ref, executed_at=exit_time,
    ))
    db.commit()

    y = portfolio.capital_yield_view(db, window_days=7)

    # Window start = now-7d. Overlap with the real exit time (now-1d) is 6 days -> 400*6=2400.
    assert y["locked_dollar_days"] == pytest.approx(2400.0, rel=0.02)
    # Using the stale last_fill_at (now-6d) would give only ~1 day -> 400 dollar-days.
    assert y["locked_dollar_days"] > 1000.0
    assert y["realized_pnl_window"] == pytest.approx(50.0)
    assert y["pct_per_locked_dollar_day"] == pytest.approx(50.0 / 2400.0 * 100, rel=0.02)


def test_capital_view_bar_disjoint_and_sums_to_100(db, monkeypatch):
    """D1: `backup` is a policy claim on `free_cash`, not a disjoint pot -- summing all four
    raw fields overlaps and can exceed 100% (500+1368+82+550 = 2500 = 125% of equity 2000),
    clipping the `deployed` segment invisible inside `.cap-bar { overflow: hidden }`. The bar
    must be built from `free_after_backup` (free_cash minus backup) so the four segments are
    disjoint and their snapped steps sum to exactly 100."""
    monkeypatch.setattr(settings, "account_equity", 2000.0)
    monkeypatch.setattr(settings, "equity_backup_pct", 25.0)
    monkeypatch.setattr(settings, "max_concurrent_sessions", 10)
    monkeypatch.setattr(settings, "scan_fund", 1000.0)
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))

    _active_session(db, "AAA", isolated_fund=600.0, total_cost=200.0)
    _active_session(db, "BBB", isolated_fund=400.0, total_cost=350.0)
    _position(db, "AAA", quantity=2.0, avg_entry_price=100.0, total_cost=200.0)
    _position(db, "BBB", quantity=3.5, avg_entry_price=100.0, total_cost=350.0)
    db.commit()
    _resting_buy(db, "CCC", quantity=10.0, price=5.0)
    _resting_buy(db, "DDD", quantity=4.0, price=8.0)

    v = portfolio.capital_view(db)
    # Raw fields overlap: backup(500) + free_cash(1368) + resting_buy(82) + deployed(550)
    # = 2500 = 125% of equity -- the pre-fix bug (the bar would clip `deployed` entirely).
    assert v["backup"] + v["free_cash"] + v["resting_buy"] + v["deployed"] == pytest.approx(2500.0)
    assert v["free_after_backup"] == pytest.approx(868.0)  # 1368 - 500, disjoint from backup

    bar = v["bar"]
    assert [seg["cls"] for seg in bar] == [
        "cap-seg-backup", "cap-seg-free", "cap-seg-resting", "cap-seg-deployed",
    ]
    assert sum(seg["step"] for seg in bar) == 100
    deployed_seg = bar[-1]
    assert deployed_seg["cls"] == "cap-seg-deployed"
    assert deployed_seg["step"] > 0  # NOT clipped invisible


def test_capital_bar_last_segment_absorbs_rounding_overflow():
    """D5: independent nearest-5% rounding of a fully disjoint, exactly-100%-raw book
    (24/24/24/28) gives 25/25/25/30 = 105 -- an overflow even with no overlap at all. The
    last segment must be set to 100 minus the sum of the already-snapped previous steps so
    the bar always sums to exactly 100."""
    bar = portfolio._capital_bar(
        equity=1000.0, backup=240.0, free_after_backup=240.0, resting_buy=240.0, deployed=280.0,
    )
    steps = [seg["step"] for seg in bar]
    assert steps[:3] == [25, 25, 25]  # each snapped independently, unchanged
    assert steps[3] == 25  # NOT 30 -- absorbs the remainder (100 - 75), not its own snap
    assert sum(steps) == 100


def test_session_exit_time_never_uses_a_later_sessions_fill(db):
    """D3: a stopped session with no own `pyramid:{id}:` SELL fill must not fall back to a
    LATER session's fill on the same symbol -- the fallback is bounded above by the next
    session's start."""
    now = utcnow()
    s1_start = now - timedelta(days=10)
    s2_start = now - timedelta(days=2)
    later_fill_time = now - timedelta(days=1)  # belongs to session 2, not session 1

    s1 = KssSession(
        symbol="NEAR", entry_price=1.0, distance_pct=2.0, max_waves=5,
        isolated_fund=100.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
        status=SESSION_STOPPED, total_cost=100.0, started_at=s1_start, created_at=s1_start,
    )
    s2 = KssSession(
        symbol="NEAR", entry_price=1.0, distance_pct=2.0, max_waves=5,
        isolated_fund=100.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
        status=SESSION_ACTIVE, total_cost=50.0, started_at=s2_start, created_at=s2_start,
    )
    db.add_all([s1, s2])
    db.commit()

    sell = PendingOrder(
        symbol="NEAR", side="SELL", order_type="MARKET", quantity=10.0, price=1.0,
        source="kss", source_ref=f"pyramid:{s2.id}:wave:0", status="executed",
    )
    db.add(sell)
    db.commit()
    db.add(Fill(
        pending_order_id=sell.id, symbol="NEAR", side="SELL", quantity=10.0, price=1.0,
        realized_pnl=5.0, source_ref=sell.source_ref, executed_at=later_fill_time,
    ))
    db.commit()

    # Pre-fix this returned `later_fill_time` (session 2's fill); bounded, it must find
    # nothing before session 2 started and return None instead.
    exit_time = portfolio._session_exit_time(db, s1, now)
    assert exit_time is None


def test_yield_counts_funded_stopped_session_without_own_exit_as_skipped(db):
    """D3: a FUNDED (total_cost > 0) stopped session with no own exit fill must be counted
    in `skipped`, not silently mis-dated to a later same-symbol session's fill (which would
    inflate/deflate dollar-days with the wrong window)."""
    now = utcnow()
    s1_start = now - timedelta(days=10)
    s2_start = now - timedelta(days=2)
    later_fill_time = now - timedelta(days=1)

    s1 = KssSession(
        symbol="NEAR", entry_price=1.0, distance_pct=2.0, max_waves=5,
        isolated_fund=100.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
        status=SESSION_STOPPED, total_cost=100.0, started_at=s1_start, created_at=s1_start,
    )
    s2 = KssSession(
        symbol="NEAR", entry_price=1.0, distance_pct=2.0, max_waves=5,
        isolated_fund=100.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
        status=SESSION_ACTIVE, total_cost=50.0, started_at=s2_start, created_at=s2_start,
    )
    db.add_all([s1, s2])
    db.commit()

    sell = PendingOrder(
        symbol="NEAR", side="SELL", order_type="MARKET", quantity=10.0, price=1.0,
        source="kss", source_ref=f"pyramid:{s2.id}:wave:0", status="executed",
    )
    db.add(sell)
    db.commit()
    db.add(Fill(
        pending_order_id=sell.id, symbol="NEAR", side="SELL", quantity=10.0, price=1.0,
        realized_pnl=5.0, source_ref=sell.source_ref, executed_at=later_fill_time,
    ))
    db.commit()

    y = portfolio.capital_yield_view(db, window_days=7)
    assert y["skipped"] >= 1


def test_realized_pnl_window_excludes_non_kss_fills(db):
    """D4: numerator and denominator must cover the same book. `locked_dollar_days` only
    ever counts KSS sessions, so `realized_pnl_window` must be restricted to fills whose
    order came from the pyramid ladder (`pyramid:`) or an orphan-inventory sweep
    (`orphan:`) -- a manual/OPUS fill must not inflate the KSS-only yield ratio."""
    now = utcnow()

    manual_order = PendingOrder(
        symbol="AAA", side="SELL", order_type="MARKET", quantity=1.0, price=100.0,
        source="manual", source_ref=None, status="executed",
    )
    db.add(manual_order)
    db.commit()
    db.add(Fill(
        pending_order_id=manual_order.id, symbol="AAA", side="SELL", quantity=1.0,
        price=100.0, realized_pnl=1000.0, source_ref=None, executed_at=now,
    ))

    kss_order = PendingOrder(
        symbol="BBB", side="SELL", order_type="MARKET", quantity=1.0, price=50.0,
        source="kss", source_ref="pyramid:1:tp", status="executed",
    )
    db.add(kss_order)
    db.commit()
    db.add(Fill(
        pending_order_id=kss_order.id, symbol="BBB", side="SELL", quantity=1.0,
        price=50.0, realized_pnl=25.0, source_ref="pyramid:1:tp", executed_at=now,
    ))

    orphan_order = PendingOrder(
        symbol="CCC", side="SELL", order_type="MARKET", quantity=1.0, price=10.0,
        source="kss", source_ref="orphan:2:sweep", status="executed",
    )
    db.add(orphan_order)
    db.commit()
    db.add(Fill(
        pending_order_id=orphan_order.id, symbol="CCC", side="SELL", quantity=1.0,
        price=10.0, realized_pnl=5.0, source_ref="orphan:2:sweep", executed_at=now,
    ))
    db.commit()

    y = portfolio.capital_yield_view(db, window_days=7)
    assert y["realized_pnl_window"] == pytest.approx(30.0)  # 25 + 5, the manual 1000 excluded


def test_capital_yield_view_query_count_is_bounded_not_n_plus_1(db):
    """D6: on a 15s-polled endpoint, the query count must not grow with the number of
    sessions (N+1) -- SELL fills and sessions are fetched once and resolved in Python."""
    from sqlalchemy import event

    from app.db import engine

    now = utcnow()
    for i in range(20):
        db.add(KssSession(
            symbol=f"SYM{i}", entry_price=1.0, distance_pct=2.0, max_waves=5,
            isolated_fund=100.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
            status=SESSION_COMPLETED, total_cost=50.0,
            started_at=now - timedelta(days=5), created_at=now - timedelta(days=5),
        ))
    db.commit()

    queries: list[str] = []

    def _count(conn, cursor, statement, *a, **kw):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        portfolio.capital_yield_view(db, window_days=7)
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    # A handful of batched SELECTs regardless of session count -- NOT ~1-2 per session
    # (which would be 20+ for the 20 sessions seeded above).
    assert len(queries) < 10


def test_partial_capital_route_returns_200_with_labels(db, client, monkeypatch):
    """Strengthened per the audit: the vacuous version asserted only template-literal
    substrings, so it passed with the bar at 125% (clipped) and every number zero. This
    asserts the rendered segment steps sum to exactly 100 (would have caught D1) and that a
    known money value from the fixture book actually appears (not a template literal)."""
    monkeypatch.setattr(settings, "account_equity", 2000.0)
    monkeypatch.setattr(settings, "equity_backup_pct", 25.0)

    r = client.get("/partials/capital")
    assert r.status_code == 200
    body = r.text
    assert "cap-bar" in body
    assert "Vốn" in body
    assert "2,000.00" in body  # c.equity | money, from the fixture -- not a template literal

    steps = [
        int(n) for n in re.findall(
            r'class="cap-seg cap-seg-(?:backup|free|resting|deployed) w-(\d+)"', body,
        )
    ]
    assert len(steps) == 4
    assert sum(steps) == 100


def test_capital_bar_residual_lands_on_the_largest_segment_not_on_deployed():
    """Snapping four segments to a 5% grid leaves a residual; absorbing it in the LAST
    segment would dump up to three roundings onto `deployed` — the number the panel exists
    to show and usually the smallest slice. The largest segment carries it invisibly."""
    # raw 25.0 / 70.0 / 4.5 / 0.5 -> snapped 25 / 70 / 5 / 0 = 100 already; nudge `free` so a
    # residual really exists and prove it does NOT come out of `deployed`.
    bar = portfolio._capital_bar(2000.0, 500.0, 1390.0, 90.0, 20.0)
    steps = {seg["cls"]: seg["step"] for seg in bar}
    assert sum(steps.values()) == 100
    # deployed is 1.0% of equity -> snaps to 0 and must stay there, not absorb the residual.
    assert steps["cap-seg-deployed"] == 0
    assert steps["cap-seg-free"] >= 65  # the largest segment took it


def test_capital_bar_is_empty_on_a_zero_equity_book():
    """A book with no equity must render an EMPTY bar, never a bar that is 100% backup."""
    bar = portfolio._capital_bar(0.0, 0.0, 0.0, 0.0, 0.0)
    assert sum(seg["step"] for seg in bar) == 0


def test_capital_bar_always_sums_to_100_on_a_disjoint_book():
    """Property: for any disjoint split of equity the snapped steps total exactly 100 and
    every step stays on the 5% grid CSS actually defines (.w-0 ... .w-100)."""
    import random

    random.seed(7)
    for _ in range(2000):
        equity = random.uniform(1.0, 1e6)
        parts = [random.random() for _ in range(4)]
        scale = equity / sum(parts)
        bar = portfolio._capital_bar(equity, *[p * scale for p in parts])
        assert sum(seg["step"] for seg in bar) == 100
        assert all(0 <= seg["step"] <= 100 and seg["step"] % 5 == 0 for seg in bar)
