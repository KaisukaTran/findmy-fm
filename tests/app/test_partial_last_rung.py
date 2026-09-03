"""Partial last rung — when the geometric next rung does not fit the session's remaining
isolated fund, queue it at the SAME target price with a REDUCED quantity sized to spend
exactly what remains, instead of stranding the ladder (today's ``insufficient_fund`` dead-end).

Owner's call (live evidence, ATOM session 16, 2026-09-03): the next rung cost $0.021 more
than the $112.8782 left in the fund, so the ladder went dead with no rung on the book at all —
the same shape that killed ONDO/WLD and was patched by hand-topping-up ``isolated_fund``. The
fix here is the opposite of a top-up: the ladder spends what it actually reserved. Both entry
points (the fill-driven auto-chain in ``handle_fill_event`` and the watchdog
``_rearm_dead_ladders``) share ONE helper, ``service._try_partial_rung``.
"""

from __future__ import annotations

import json

import pytest

from app import orders
from app.config import settings
from app.kss import service
from app.models import (
    SESSION_ACTIVE,
    WAVE_FILLED,
    WAVE_SENT,
    AuditLog,
    KssSession,
    KssWave,
    PendingOrder,
)


@pytest.fixture
def mock_market(monkeypatch):
    ex_info = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: ex_info)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {})
    monkeypatch.setattr("app.market.get_current_prices", lambda syms, force=False: {})
    # app.orders imports get_current_prices BY NAME at module load, so the patch above (on the
    # app.market attribute) never reaches it — the paper fill path (_paper_execute/_apply_cash_cap)
    # would otherwise try a REAL network lookup for a real symbol like ATOM. Patch the bound name
    # too so every fill in these tests is fully offline and deterministic.
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms, force=False: {})


def _session(db, **kw) -> KssSession:
    """A session already sitting at ``current_wave`` with the wave(s) up to it plausibly
    filled. Auto-seeds the KssWave row for ``current_wave`` (when >=1) so
    ``_anchor_dca_price`` finds a real 'previous wave' to chain the next rung's price from,
    instead of silently falling back to entry_price (which would only be correct for wave 1)."""
    defaults = {
        "symbol": "AAA", "entry_price": 100.0, "distance_pct": 2.0, "max_waves": 6,
        "isolated_fund": 1_000_000.0, "tp_pct": 50.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0,
        "status": SESSION_ACTIVE, "current_wave": 1, "avg_price": 100.0,
        "total_filled_qty": 1.0, "total_cost": 100.0, "sl_pct": 0.0, "strategy_mode": "dca_down",
        "first_wave_usd": 15.0,
    }
    defaults.update(kw)
    row = KssSession(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    if row.current_wave >= 1:
        py = service._to_pyramid(row)
        w = py.generate_wave(row.current_wave)
        db.add(KssWave(
            session_id=row.id, wave_num=row.current_wave, quantity=w.quantity,
            target_price=w.target_price, status=WAVE_FILLED,
            filled_qty=w.quantity, filled_price=w.target_price,
        ))
        db.commit()
    return row


def _wave(row: KssSession, wave_num: int):
    """The FULL (un-shrunk) rung at ``wave_num``, straight from the frozen ladder math — so
    test numbers never depend on live settings globals."""
    py = service._to_pyramid(row)
    return py.generate_wave(wave_num)


def _pending_waves(db, session_id: int) -> list[PendingOrder]:
    return (
        db.query(PendingOrder)
        .filter(PendingOrder.source_ref.like(f"pyramid:{session_id}:wave:%"))
        .all()
    )


def _audits(db, action: str, session_id: int) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.action == action, AuditLog.entity == f"kss:{session_id}")
        .all()
    )


# --- 1. the exact ATOM shortfall, via the real fill-driven pipeline --------------------


def test_atom_shortfall_queues_a_partial_rung_at_the_same_price(db, mock_market):
    """Reproduces ATOM session 16 exactly: entry/distance/first_wave_usd chosen so wave 0 +
    wave 1 fill to total_cost=$117.6171 (remaining $112.8782 of a $230.49528507 fund) and
    wave 2's full rung needs $112.8992 — a $0.021 shortfall. A partial rung must be queued at
    wave 2's real (unchanged) target price, sized to fit the remaining fund."""
    row = service.create_session(
        db, symbol="ATOM", entry_price=4.62, distance_pct=2.9799817998238276, max_waves=6,
        isolated_fund=230.49528507, tp_pct=90.0, timeout_x_min=1440.0, gap_y_min=0.0,
        first_wave_usd=39.98037895748244,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])  # wave 0 fills -> queues wave 1
    w1 = next(p for p in orders.list_pending(db) if p.source_ref == f"pyramid:{row.id}:wave:1")
    orders.approve_order(db, w1.id)  # wave 1 fills -> wave 2 needed, insufficient by $0.021

    db.refresh(row)
    assert row.total_cost == pytest.approx(117.6171, abs=0.001)
    remaining = row.isolated_fund - row.total_cost
    assert remaining == pytest.approx(112.8782, abs=0.001)

    full = _wave(row, 2)
    full_cost = full.quantity * full.target_price
    assert full_cost == pytest.approx(112.8992, abs=0.001)
    assert full_cost > remaining  # the real shortfall

    queued = [p for p in orders.list_pending(db) if p.source_ref == f"pyramid:{row.id}:wave:2"]
    assert len(queued) == 1, "a partial rung must be queued instead of nothing"
    wave2 = queued[0]
    assert wave2.side == "BUY"
    # Price is untouched: the SAME target_price is fed into the queue as a full-size rung
    # would carry. The tolerance (not 1e-8) absorbs a pre-existing, unrelated precision quirk
    # already present for every multi-wave rung: _anchor_dca_price re-rounds to 8dp off the
    # PREVIOUS wave's own already-rounded price, while generate_wave rounds straight from
    # entry_price to 6dp (ATOM's precision, entry<100) — the two chains drift by lt 1e-6 in
    # price, ~$0.0000004/unit, financially immaterial and unchanged by this feature.
    assert wave2.price == pytest.approx(full.target_price, abs=1e-5), "price must be untouched"
    assert wave2.quantity < full.quantity, "quantity must be shrunk"
    # +1e-3 (not 1e-9): the SAME pre-existing anchor-precision drift noted above can nudge the
    # final stored price a few 1e-7 above the raw price the quantity was sized against, which
    # multiplied by qty is worth well under a thousandth of a dollar here — immaterial.
    # STRICT: the whole point is that the rung fits the fund it was reserved from. No epsilon
    # here — sizing now uses the anchored (actually-queued) price, so this is exact.
    assert wave2.quantity * wave2.price <= remaining

    partial_audits = _audits(db, "partial_rung", row.id)
    assert len(partial_audits) == 1
    detail = json.loads(partial_audits[0].detail)
    assert detail["symbol"] == "ATOM"
    assert detail["wave"] == 2
    assert detail["full_cost"] == pytest.approx(112.8992, abs=0.001)
    assert detail["partial_cost"] == pytest.approx(wave2.quantity * wave2.price, abs=0.001)
    assert detail["remaining_fund"] == pytest.approx(112.8782, abs=0.001)
    assert detail["partial_qty"] < detail["full_qty"]

    assert _audits(db, "insufficient_fund", row.id) == []


# --- 2. shortfall too large: falls under minQty/minNotional -> today's behaviour -----------


def test_shortfall_too_large_falls_back_to_insufficient_fund(db, mock_market):
    row = _session(db)
    full = _wave(row, 2)
    full_cost = full.quantity * full.target_price
    row.isolated_fund = row.total_cost + settings.scan_min_notional * 0.1  # tiny dust remainder
    db.commit()
    remaining = row.isolated_fund - row.total_cost
    assert full_cost > remaining  # a real shortfall
    assert remaining < settings.scan_min_notional  # too small to clear the notional floor

    service._rearm_dead_ladders(db)

    assert _pending_waves(db, row.id) == []
    assert _audits(db, "partial_rung", row.id) == []
    insuff = _audits(db, "insufficient_fund", row.id)
    assert len(insuff) == 1


# --- 3. knob off -> exactly today's behaviour -----------------------------------------------


def test_knob_off_is_todays_behavior(db, mock_market, monkeypatch):
    monkeypatch.setattr(settings, "kss_partial_last_rung_enabled", False)
    row = _session(db)
    full = _wave(row, 2)
    remaining = full.quantity * full.target_price * 0.5  # would clear the floor if partial were ON
    row.isolated_fund = row.total_cost + remaining
    db.commit()

    service._rearm_dead_ladders(db)

    assert _pending_waves(db, row.id) == []
    assert _audits(db, "partial_rung", row.id) == []
    assert len(_audits(db, "insufficient_fund", row.id)) == 1


# --- 4. a rung that fits normally is untouched ----------------------------------------------


def test_a_rung_that_fits_normally_is_untouched(db, mock_market):
    row = _session(db)
    full = _wave(row, 2)
    row.isolated_fund = row.total_cost + full.quantity * full.target_price * 2  # comfortable
    db.commit()

    service._rearm_dead_ladders(db)

    waves = _pending_waves(db, row.id)
    assert len(waves) == 1
    assert waves[0].quantity == pytest.approx(full.quantity)
    assert _audits(db, "partial_rung", row.id) == []


# --- 5. watchdog and fill-driven path produce the SAME partial rung (one shared helper) -----


def test_watchdog_and_fill_path_produce_the_same_partial_rung(db, mock_market):
    shared = {
        "symbol": "AAA", "entry_price": 100.0, "distance_pct": 2.0, "max_waves": 6,
        "tp_pct": 50.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0, "sl_pct": 0.0,
        "strategy_mode": "dca_down", "first_wave_usd": 15.0,
    }
    tmpl = KssSession(current_wave=0, total_cost=0.0, total_filled_qty=0.0, avg_price=0.0,
                       isolated_fund=1_000_000.0, status=SESSION_ACTIVE, **shared)
    db.add(tmpl)
    db.commit()
    db.refresh(tmpl)
    py = service._to_pyramid(tmpl)
    w0, w1, w2 = py.generate_wave(0), py.generate_wave(1), py.generate_wave(2)
    cost0 = w0.quantity * w0.target_price
    cost1 = w1.quantity * w1.target_price
    total01 = cost0 + cost1
    remaining = (w2.quantity * w2.target_price) * 0.5
    isolated_fund = total01 + remaining

    # A: watchdog path — both fills already booked, current_wave already at 1.
    row_a = KssSession(current_wave=1, total_cost=total01, total_filled_qty=w0.quantity + w1.quantity,
                        avg_price=total01 / (w0.quantity + w1.quantity), isolated_fund=isolated_fund,
                        status=SESSION_ACTIVE, **shared)
    db.add(row_a)
    db.commit()
    db.refresh(row_a)
    db.add(KssWave(session_id=row_a.id, wave_num=1, quantity=w1.quantity, target_price=w1.target_price,
                    status=WAVE_FILLED, filled_qty=w1.quantity, filled_price=w1.target_price))
    db.commit()

    service._rearm_dead_ladders(db)
    wave_a = _pending_waves(db, row_a.id)[0]

    # B: fill-driven path — wave 0 filled, wave 1 resting, fill it now via handle_fill_event.
    row_b = KssSession(current_wave=0, total_cost=cost0, total_filled_qty=w0.quantity,
                        avg_price=w0.target_price, isolated_fund=isolated_fund,
                        status=SESSION_ACTIVE, **shared)
    db.add(row_b)
    db.commit()
    db.refresh(row_b)
    db.add(KssWave(session_id=row_b.id, wave_num=0, quantity=w0.quantity, target_price=w0.target_price,
                    status=WAVE_FILLED, filled_qty=w0.quantity, filled_price=w0.target_price))
    db.add(KssWave(session_id=row_b.id, wave_num=1, quantity=w1.quantity, target_price=w1.target_price,
                    status=WAVE_SENT))
    db.commit()

    service.handle_fill_event(db, f"pyramid:{row_b.id}:wave:1", w1.quantity, w1.target_price)
    wave_b = _pending_waves(db, row_b.id)[0]

    assert wave_a.quantity == pytest.approx(wave_b.quantity, abs=1e-9)
    assert wave_a.price == pytest.approx(wave_b.price, abs=1e-9)
    assert wave_a.quantity < w2.quantity  # actually shrunk on both sides


# --- 6. dedupe: three manage passes -> exactly one partial_rung audit, one queued rung ------


def test_partial_rung_dedupes_across_manage_passes(db, mock_market):
    row = _session(db)
    full = _wave(row, 2)
    remaining = full.quantity * full.target_price * 0.5
    row.isolated_fund = row.total_cost + remaining
    db.commit()

    service.manage_open_sessions(db)
    service.manage_open_sessions(db)
    service.manage_open_sessions(db)

    assert len(_pending_waves(db, row.id)) == 1
    assert len(_audits(db, "partial_rung", row.id)) == 1


# --- 7. a partial rung _queue_wave_if_above_sl refuses (below SL) queues nothing -----------


def test_partial_rung_refused_below_sl_queues_nothing(db, mock_market):
    row = _session(db, sl_pct=1.0)  # avg_price=100 -> floor=99; anchored wave-2 price=96.04
    full = _wave(row, 2)
    remaining = full.quantity * full.target_price * 0.5  # would clear the notional floor
    row.isolated_fund = row.total_cost + remaining
    db.commit()

    service._rearm_dead_ladders(db)

    assert _pending_waves(db, row.id) == []
    assert _audits(db, "partial_rung", row.id) == []
    assert len(_audits(db, "wave_below_sl", row.id)) == 1
