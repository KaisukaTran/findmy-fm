"""kss_reconcile_interval_sec: split the guard's reconcile cadence from its exit-check cadence.

`_guard_once` (the `kss_exit_check_sec` loop, 90s by default) used to reconcile live orders on
EVERY tick — but the exit check itself is free (prices come from the live WS feed cache, see
`market.get_current_prices(force=True)`), while `orders.reconcile_live_orders` costs exchange
weight (`fetch_order` = 4 per tracked order). That coupling meant a shorter, cheap exit-check
interval could only be bought at the price of proportionally more reconcile weight.

`kss_reconcile_interval_sec` (default 300s, 0 = every tick / the old behaviour) throttles the
reconcile step on its OWN cadence inside `_guard_once`, while `run_position_guard`'s exit check
still runs every tick regardless. The C2 invariant is unchanged: on the ticks where reconcile
DOES run, it still runs before `run_position_guard` so hard-SL decisions are never sized off a
stale `total_filled_qty`. See `tests/app/test_guard_reconcile.py` for the invariant this builds
on (backlog cap, exception swallowing, the `_last_guard_at` liveness stamp).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import orders, scheduler
from app.config import settings
from app.kss import service
from app.main import app as fastapi_app
from app.models import PENDING, PendingOrder


@pytest.fixture
def client():
    with TestClient(fastapi_app) as c:
        yield c


def test_interval_zero_reconciles_every_tick_before_the_guard(db, monkeypatch):
    """0 = the old, undifferentiated behaviour: reconcile runs every tick, before the guard."""
    monkeypatch.setattr(settings, "kss_reconcile_interval_sec", 0)
    calls: list[str] = []
    monkeypatch.setattr(orders, "reconcile_live_orders", lambda db: calls.append("reconcile") or [])
    monkeypatch.setattr(service, "run_position_guard", lambda db: calls.append("guard") or {})

    scheduler._guard_once()
    scheduler._guard_once()

    assert calls == ["reconcile", "guard", "reconcile", "guard"], (
        "interval 0 must reconcile on every tick, and always before the guard's exit checks"
    )


def test_second_tick_within_the_interval_skips_reconcile_but_the_guard_still_runs(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_reconcile_interval_sec", 300)
    calls: list[str] = []
    monkeypatch.setattr(orders, "reconcile_live_orders", lambda db: calls.append("reconcile") or [])
    monkeypatch.setattr(service, "run_position_guard", lambda db: calls.append("guard") or {})

    scheduler._guard_once()  # never reconciled before -> due -> reconciles
    assert calls == ["reconcile", "guard"]

    calls.clear()
    scheduler._guard_once()  # same instant (real clock barely moves) -> interval not elapsed

    assert calls == ["guard"], "reconcile must be skipped within the interval, but the guard must still run"


def test_reconcile_runs_again_once_the_interval_has_elapsed_still_before_the_guard(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_reconcile_interval_sec", 300)
    calls: list[str] = []
    monkeypatch.setattr(orders, "reconcile_live_orders", lambda db: calls.append("reconcile") or [])
    monkeypatch.setattr(service, "run_position_guard", lambda db: calls.append("guard") or {})

    t0 = datetime(2026, 1, 1, 0, 0, 0)
    clock = {"now": t0}
    monkeypatch.setattr(scheduler, "utcnow", lambda: clock["now"])

    scheduler._guard_once()  # t0: never reconciled -> due
    assert calls == ["reconcile", "guard"]

    calls.clear()
    clock["now"] = t0 + timedelta(seconds=100)
    scheduler._guard_once()  # +100s: interval (300s) not elapsed -> skip
    assert calls == ["guard"]

    calls.clear()
    clock["now"] = t0 + timedelta(seconds=301)
    scheduler._guard_once()  # +301s: interval elapsed -> reconciles again, before the guard
    assert calls == ["reconcile", "guard"]


def test_a_reconcile_exception_is_swallowed_and_the_guard_still_runs(db, monkeypatch):
    """Same invariant as `test_guard_reconcile.py::test_a_reconcile_exception_does_not_stop_the_guard`,
    exercised with the interval knob set — a failure must not stamp `_last_reconcile_at`, so the
    next tick retries rather than being throttled off by a "successful" timestamp that never
    happened."""
    monkeypatch.setattr(settings, "kss_reconcile_interval_sec", 300)

    def _boom(db):
        raise RuntimeError("exchange unreachable")

    guard_calls: list[str] = []
    monkeypatch.setattr(orders, "reconcile_live_orders", _boom)
    monkeypatch.setattr(service, "run_position_guard", lambda db: guard_calls.append("guard") or {})

    scheduler._guard_once()  # must not raise

    assert guard_calls == ["guard"], "the guard's exit checks must run regardless"
    assert scheduler._last_reconcile_at is None, "a failed reconcile must not be recorded as done"


def test_backlog_cap_skips_reconcile_independently_of_the_interval(db, monkeypatch):
    """GUARD_RECONCILE_MAX_ORDERS must still apply even when the interval alone would allow a
    reconcile every tick (interval=0)."""
    monkeypatch.setattr(settings, "kss_reconcile_interval_sec", 0)
    for i in range(scheduler.GUARD_RECONCILE_MAX_ORDERS + 1):
        db.add(PendingOrder(symbol="AAA", side="BUY", order_type="LIMIT", quantity=1.0,
                            price=1.0, status=PENDING, source="kss",
                            source_ref=f"pyramid:1:wave:{i}", exchange_order_id=f"X{i}",
                            exchange_status="open"))
    db.commit()
    calls: list[str] = []
    monkeypatch.setattr(orders, "reconcile_live_orders", lambda db: calls.append("reconcile") or [])
    monkeypatch.setattr(service, "run_position_guard", lambda db: calls.append("guard") or {})

    scheduler._guard_once()

    assert calls == ["guard"], "the backlog cap must skip reconcile even when the interval alone allows it"


def test_health_reports_reconcile_seconds_ago(client):
    body = client.get("/health").json()

    assert "reconcile_seconds_ago" in body


def test_the_shipped_default_keeps_todays_cadence(db, monkeypatch):
    """The knob must not change live behaviour the day it ships: with the default interval and
    the default 90 s exit check, EVERY guard tick still reconciles. The default sits below the
    exit-check default precisely so that holds — and so a later, shorter exit check does not
    drag reconcile (and its fetch_order weight) down with it."""
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["kss_reconcile_interval_sec"].default < fields["kss_exit_check_sec"].default, (
        "a reconcile interval at or above the guard tick would skip ticks on timing jitter"
    )
