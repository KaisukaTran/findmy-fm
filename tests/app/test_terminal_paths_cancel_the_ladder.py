"""A session that has ended must not leave BUY rungs resting on the exchange.

Under the 1.5 resting model an ACTIVE session deliberately keeps its unfilled DCA rungs and
its take-profit ON the venue. So every path that ends a session has to take the ladder back
off, or the exchange is left holding live BUY orders for a session that no longer exists: they
lock quote balance, and if the market dips to one it buys a position with no session, no
ladder, no take-profit and no stop — the orphan pattern behind much of our realised loss.

I fixed the `:tp` branch of `handle_fill_event` earlier and wrote in the comment that it "was
the only terminal path that did not". **That was wrong.** The very next branch — the risk
exits, `sl` / `trailing` / `trail_sl` / `deadline` — sets the session STOPPED and returns
without touching the ladder. It is the worse of the two: a stop-loss fires in a FALLING
market, which is exactly when the resting BUY rungs below the price are about to be hit. We
sell to cap the loss and the venue immediately buys us back in, unmanaged, into the fall.

Verified live on the testnet soak (2026-08-30): sessions 6 (DOT) and 7 (NEAR) were COMPLETED
while Binance still showed their rungs open — order 218090 buy 36 DOT @ 0.822 and 343128 buy
17 NEAR @ 1.767, $59.63 of unmanaged exposure. Those two predate the `:tp` fix, which is the
second hole this covers: nothing ever cleans up an orphan that already exists. Marking the row
REJECTED is enough to retire it, because `orders.sync_resting_orders` reaps a REJECTED row
from the venue on its next pass (app/orders.py:846).
"""

from __future__ import annotations

import pytest

from app import models
from app.kss import service as kss
from app.models import PENDING, REJECTED, PendingOrder


@pytest.fixture(autouse=True)
def _resting_model_on(monkeypatch):
    """Every rung below is one the 1.5 model has already rested on the venue, so the pass that
    takes a REJECTED row back off the exchange has to be live for the sweep to be allowed to
    retire anything. One test deliberately turns this back off."""
    monkeypatch.setattr("app.orders.resting_model_active", lambda: True)


def _session(db, **kw) -> models.KssSession:
    defaults = {
        "symbol": "DOT", "entry_price": 1.0, "distance_pct": 2.0, "max_waves": 4,
        "isolated_fund": 140.0, "tp_pct": 3.0, "timeout_x_min": 60, "gap_y_min": 5,
        "status": models.SESSION_ACTIVE, "current_wave": 1, "avg_price": 1.0,
        "total_filled_qty": 18.0, "total_cost": 18.0, "sl_pct": 8.0,
    }
    defaults.update(kw)
    row = models.KssSession(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _rung(db, session_id: int, wave: int = 1) -> PendingOrder:
    """A DCA rung already resting on the venue, exactly as the 1.5 model leaves it."""
    o = PendingOrder(symbol="DOT", side="BUY", order_type="LIMIT", quantity=36.0, price=0.822,
                     source="kss", source_ref=f"pyramid:{session_id}:wave:{wave}", status=PENDING,
                     exchange_order_id="218090", exchange_status="open")
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _still_pending(db, session_id: int) -> list[PendingOrder]:
    return [o for o in db.query(PendingOrder).filter(PendingOrder.status == PENDING).all()
            if str(o.source_ref or "").startswith(f"pyramid:{session_id}:wave:")]


# --- every terminal path retires the ladder ---------------------------------

@pytest.mark.parametrize("exit_kind", ["sl", "trailing", "trail_sl", "deadline"])
def test_a_risk_exit_takes_the_ladder_off_the_exchange(db, exit_kind):
    """The dangerous case: we stop out as the price falls, and the rungs sit right below it."""
    row = _session(db)
    rung = _rung(db, row.id)

    kss.handle_fill_event(db, f"pyramid:{row.id}:{exit_kind}", filled_qty=18.0, filled_price=0.9)

    db.refresh(row)
    db.refresh(rung)
    assert row.status == models.SESSION_STOPPED
    assert rung.status == REJECTED, "a stopped session must not keep buying"
    assert _still_pending(db, row.id) == []


def test_a_take_profit_still_takes_the_ladder_off(db):
    """Regression guard on the fix that came first."""
    row = _session(db)
    rung = _rung(db, row.id)

    kss.handle_fill_event(db, f"pyramid:{row.id}:tp", filled_qty=18.0, filled_price=1.03)

    db.refresh(row)
    db.refresh(rung)
    assert row.status == models.SESSION_COMPLETED
    assert rung.status == REJECTED


def test_a_partial_take_profit_keeps_the_ladder(db):
    """A partial exit does NOT end the session — it still holds a position that the ladder
    and the take-profit are managing, so nothing may be retired."""
    row = _session(db)
    rung = _rung(db, row.id)

    kss.handle_fill_event(db, f"pyramid:{row.id}:tp", filled_qty=8.0, filled_price=1.03)

    db.refresh(row)
    db.refresh(rung)
    assert row.status == models.SESSION_ACTIVE
    assert rung.status == PENDING, "the session is still open — the ladder stays"


# --- and an orphan that already exists gets swept ----------------------------

def test_a_rung_left_over_from_an_ended_session_is_swept(db):
    """Self-healing. The two live orphans on the soak predate the fix above, so fixing the
    branch alone would leave them resting on Binance forever."""
    ended = _session(db, status=models.SESSION_COMPLETED)
    orphan = _rung(db, ended.id)

    kss.sweep_orphan_waves(db)

    db.refresh(orphan)
    assert orphan.status == REJECTED


@pytest.mark.parametrize("alive", [
    models.SESSION_ACTIVE,      # the resting model's whole point: rungs stay on the book
    models.SESSION_PENDING,     # created, not started yet — its ladder is still to come
    models.SESSION_TP_TRIGGERED,
])
def test_the_sweep_leaves_a_session_that_has_not_ended_alone(db, alive):
    """"Not ACTIVE" is NOT the same as "ended", and reading it that way loses money.

    `tp_triggered` is transient: `_handle_tp_triggered` puts the session straight back to
    ACTIVE when K-2 defers the sell (service.py:827). Sweeping it in that window would return
    a live session to ACTIVE with its DCA ladder silently gone, unable to average down for the
    rest of its life. `pending` is a session whose wave 0 is still being queued. Only STOPPED
    and COMPLETED are terminal.
    """
    live = _session(db, status=alive)
    rung = _rung(db, live.id)

    kss.sweep_orphan_waves(db)

    db.refresh(rung)
    assert rung.status == PENDING


def test_the_sweep_reports_what_it_retired(db):
    ended = _session(db, status=models.SESSION_STOPPED)
    _rung(db, ended.id, wave=1)
    _rung(db, ended.id, wave=2)

    assert kss.sweep_orphan_waves(db) == 2


def test_a_rung_whose_session_was_deleted_is_swept(db):
    """`delete_session` hard-deletes any non-ACTIVE session and `KssWave` cascades, but the
    PendingOrder is joined only by the `source_ref` STRING, so it survives as PENDING. An
    operator tidying stopped sessions in the UI is the most likely way an orphan is born, so a
    predicate that only matched STOPPED/COMPLETED rows would miss exactly the case this sweep
    exists for. Anything not belonging to a session that is still going is an orphan.
    """
    orphan = _rung(db, 4242)  # no KssSession row with this id at all

    assert kss.sweep_orphan_waves(db) == 1

    db.refresh(orphan)
    assert orphan.status == REJECTED


def test_a_rung_the_venue_may_already_have_filled_is_left_alone(db):
    """PENDING + a live link + a TERMINAL exchange status is a real state: `_live_execute`
    stamps the status and can raise before booking the fill. Its only recovery is the dead-link
    reaper, which looks for PENDING rows — flipping this one to REJECTED would drop it out of
    that loop, out of reconcile, and leave us holding base asset we never recorded.
    """
    ended = _session(db, status=models.SESSION_COMPLETED)
    rung = _rung(db, ended.id)
    rung.exchange_status = "closed"          # terminal, possibly with an unbooked fill
    db.commit()

    kss.sweep_orphan_waves(db)

    db.refresh(rung)
    assert rung.status == PENDING, "let the reaper book it; do not strip its only handle"


def test_nothing_is_retired_that_the_reaper_could_not_then_cancel(db, monkeypatch):
    """Marking a row REJECTED only retires it because `sync_resting_orders` then takes it off
    the venue — and that returns immediately unless the resting model is on. With it off, a
    REJECTED row with a live link is an order still working at Binance that the app has thrown
    away its handle on."""
    monkeypatch.setattr("app.orders.resting_model_active", lambda: False)
    ended = _session(db, status=models.SESSION_STOPPED)
    rung = _rung(db, ended.id)

    kss.sweep_orphan_waves(db)

    db.refresh(rung)
    assert rung.status == PENDING


def test_the_sweep_never_touches_an_exit(db):
    """A queued SELL for an ended session is the thing still closing the position. Retiring it
    would be the one unforgivable bug in this project."""
    ended = _session(db, status=models.SESSION_STOPPED)
    sell = PendingOrder(symbol="DOT", side="SELL", order_type="MARKET", quantity=18.0, price=0.0,
                        source="kss", source_ref=f"pyramid:{ended.id}:sl", status=PENDING)
    db.add(sell)
    db.commit()

    kss.sweep_orphan_waves(db)

    db.refresh(sell)
    assert sell.status == PENDING, "an exit is never cancelled"


def test_a_sell_shaped_like_a_wave_is_still_never_cancelled(db):
    """Today every `…:wave:%` row happens to be a BUY, so the invariant holds by convention
    alone. Make it structural: selecting on the side costs nothing and removes the possibility
    that some future wave-shaped SELL gets swept."""
    ended = _session(db, status=models.SESSION_STOPPED)
    sell = PendingOrder(symbol="DOT", side="SELL", order_type="LIMIT", quantity=18.0, price=1.1,
                        source="kss", source_ref=f"pyramid:{ended.id}:wave:2", status=PENDING)
    db.add(sell)
    db.commit()

    kss.sweep_orphan_waves(db)

    db.refresh(sell)
    assert sell.status == PENDING
