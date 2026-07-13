"""Pyramid-UP defensive-DCA rung + reversal-flip to dca_down.

When a pyramid_up base goes underwater, ONE defensive DCA rung below avg fills, averages the
position down once, and flips the session to dca_down (which then keeps averaging + arms trailing
on recovery — the existing dca_down behaviour). Guards the capital-critical re-seed: the flip must
NOT double-count the held quantity (that would oversell on the next TP/SL).

Prices/ATR monkeypatched; no network. Mirrors test_pyramid_up_lifecycle.py.
"""

from __future__ import annotations

import pytest

from app import market, scanner
from app.config import settings
from app.kss import service
from app.models import (
    SESSION_ACTIVE,
    WAVE_ARMED,
    WAVE_FILLED,
    AuditLog,
    KssSession,
    KssWave,
    PendingOrder,
)

DEF = service.DEFENSIVE_WAVE_NUM  # -1


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)
    monkeypatch.setattr(settings, "kss_exit_fee_mult", 3.0)
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", True)
    monkeypatch.setattr(settings, "kss_trail_arm_pct", 5.0)
    monkeypatch.setattr(settings, "scan_distance_pct", 1.5)
    monkeypatch.setattr(settings, "scan_max_waves", 6)
    monkeypatch.setattr(settings, "sl_pct", 15.0)
    monkeypatch.setattr(settings, "kss_first_wave_usd", 1500.0)
    monkeypatch.setattr(service, "_session_atr_pct", lambda sym: 6.0)
    monkeypatch.setattr(scanner, "_symbol_at_cap", lambda db, sym: False)
    monkeypatch.setattr(scanner, "_can_open", lambda db, need: (True, ""))


def _price(monkeypatch, px):
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": px})


def _pyr_session(db, base_qty=8000.0, base_px=0.0250):
    """pyramid_up session: base (wave 0) FILLED, 2 ARMED up-adds, 1 ARMED defensive below avg."""
    s = KssSession(
        symbol="AAA", entry_price=base_px, distance_pct=2.0, max_waves=3, isolated_fund=1000.0,
        tp_pct=4.0, timeout_x_min=43200.0, gap_y_min=0.0, status=SESSION_ACTIVE, current_wave=0,
        avg_price=base_px, total_filled_qty=base_qty, total_cost=base_qty * base_px,
        peak_price=0.0, sl_pct=15.0, strategy_mode="pyramid_up", trail_active=False)
    db.add(s)
    db.commit()
    db.refresh(s)
    db.add(KssWave(session_id=s.id, wave_num=0, quantity=base_qty, target_price=base_px,
                   status=WAVE_FILLED, filled_qty=base_qty, filled_price=base_px))
    db.add(KssWave(session_id=s.id, wave_num=1, quantity=5000.0, target_price=base_px * 1.02,
                   status=WAVE_ARMED))
    db.add(KssWave(session_id=s.id, wave_num=2, quantity=3500.0, target_price=base_px * 1.04,
                   status=WAVE_ARMED))
    # defensive rung at avg×(1-1.5%)
    db.add(KssWave(session_id=s.id, wave_num=DEF, quantity=8000.0, target_price=base_px * 0.985,
                   status=WAVE_ARMED))
    db.commit()
    return s


def test_defensive_fill_flips_to_dca_without_doubling_qty(db, monkeypatch):
    """The capital-critical guard: after the defensive rung fills and the session flips to
    dca_down, total_filled_qty must equal base+defensive — NOT 2x (a double-count would oversell)."""
    s = _pyr_session(db, base_qty=8000.0, base_px=0.0250)
    def_px = 0.0246
    service.handle_fill_event(db, f"pyramid:{s.id}:wave:{DEF}", 8000.0, def_px)
    db.refresh(s)
    assert s.strategy_mode == "dca_down"                      # flipped
    assert abs(s.total_filled_qty - 16000.0) < 1e-6, s.total_filled_qty   # base 8000 + def 8000, NOT 32000
    expected_cost = 8000 * 0.0250 + 8000 * def_px
    assert abs(s.total_cost - expected_cost) < 1e-6
    assert abs(s.avg_price - expected_cost / 16000.0) < 1e-9
    # up-adds cancelled
    armed_up = db.query(KssWave).filter(KssWave.session_id == s.id, KssWave.wave_num >= 1,
                                        KssWave.status == WAVE_ARMED).count()
    assert armed_up == 0


def test_arm_cancels_defensive_rung(db, monkeypatch):
    """When a pyramid_up arms trailing (commits to riding up), the defensive rung is cancelled."""
    s = _pyr_session(db, base_qty=8000.0, base_px=0.0250)
    _price(monkeypatch, 0.0250 * 1.06)   # +6% ≥ arm → arms; _cancel_pending_waves kills ARMED waves
    service.manage_open_sessions(db)
    db.refresh(s)
    armed = db.query(KssWave).filter(KssWave.session_id == s.id,
                                     KssWave.status == WAVE_ARMED).count()
    assert armed == 0 and s.trail_active is True


def test_flipped_session_arms_trailing_on_recovery(db, monkeypatch):
    """After the flip to dca_down, a recovery still arms the dynamic trailing (no new code path)."""
    s = _pyr_session(db, base_qty=8000.0, base_px=0.0250)
    service.handle_fill_event(db, f"pyramid:{s.id}:wave:{DEF}", 8000.0, 0.0246)
    db.refresh(s)
    avg = s.avg_price
    _price(monkeypatch, avg * 1.06)      # +6% above the new avg → arms
    service.manage_open_sessions(db)
    db.refresh(s)
    assert s.trail_active is True


def test_defensive_rung_placed_at_arm_pct_below_entry(db, monkeypatch):
    """The defensive rung must sit at -kss_trail_arm_pct (5%) below entry — symmetric with the
    +arm uptrend confirmation — NOT the hair-trigger 1.5% DCA spacing."""
    from app import market
    monkeypatch.setattr(market, "get_exchange_info",
                        lambda sym: {"stepSize": 0.0001, "minQty": 0.0001})
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": 1.0})
    monkeypatch.setattr(settings, "scan_fund", 1500.0)
    monkeypatch.setattr(settings, "pyramid_up_max_adds", 2)
    monkeypatch.setattr(settings, "pyramid_up_step_pct", 2.0)
    monkeypatch.setattr(settings, "pyramid_up_size_ratio", 0.7)
    row = service.create_pyramid_up_session(db, symbol="AAA", entry_price=1.0, tp_pct=4.0,
                                            deadline_days=30)
    service.start_pyramid_up_session(db, row.id)
    defw = db.query(KssWave).filter(KssWave.session_id == row.id,
                                    KssWave.wave_num == DEF).one()
    assert abs(defw.target_price - 1.0 * (1 - settings.kss_trail_arm_pct / 100.0)) < 1e-6


def test_defensive_skipped_in_confirmed_downtrend(db, monkeypatch):
    """#2 — in a confirmed downtrend, the defensive must NOT fire (don't average into a dump);
    the wave stays ARMED and the hard SL cuts it instead."""
    s = _pyr_session(db)
    monkeypatch.setattr(service, "_coin_in_downtrend", lambda sym: True)
    service._maybe_queue_pyramid_defensive(db, s, 0.0250 * 0.98)   # market <= defensive target
    assert db.query(PendingOrder).filter(
        PendingOrder.source_ref == f"pyramid:{s.id}:wave:{DEF}").count() == 0
    defw = db.query(KssWave).filter(KssWave.session_id == s.id, KssWave.wave_num == DEF).one()
    assert defw.status == WAVE_ARMED


def test_defensive_fires_when_not_downtrend(db, monkeypatch):
    """A non-downtrend dip still flips + averages (where dca_down earns its keep)."""
    s = _pyr_session(db)
    monkeypatch.setattr(service, "_coin_in_downtrend", lambda sym: False)
    service._maybe_queue_pyramid_defensive(db, s, 0.0250 * 0.98)
    assert db.query(PendingOrder).filter(
        PendingOrder.source_ref == f"pyramid:{s.id}:wave:{DEF}").count() == 1


def test_defensive_fires_despite_symbol_at_cap(db, monkeypatch):
    """REGRESSION (the BICO bug): the defensive must fire even when _symbol_at_cap is True — the
    session occupies its OWN per-symbol slot, so averaging it is not a new open. The old code
    re-asserted the cap and blocked the defensive on every session (it never once fired)."""
    s = _pyr_session(db)
    monkeypatch.setattr(service, "_coin_in_downtrend", lambda sym: False)
    monkeypatch.setattr(scanner, "_symbol_at_cap", lambda db, sym: True)
    service._maybe_queue_pyramid_defensive(db, s, 0.0250 * 0.98)
    assert db.query(PendingOrder).filter(
        PendingOrder.source_ref == f"pyramid:{s.id}:wave:{DEF}").count() == 1


# --- N1/N2 fix: defensive rung sizing + isolated_fund reservation (ZBT #570 postmortem) --------
#
# The defensive rung used to be a FLAT ``kss_first_wave_usd`` slug regardless of the base wave's
# actual (possibly budget-shrunk) size, and the session's ``isolated_fund`` reservation only ever
# summed the up-ladder — never the defensive. ZBT #570: base ~$467, defensive filled ~$1,496 (3.2x
# the base), total_cost 196% of isolated_fund, then an 8.6-day ride to the hard SL for -$302.


def _create_started(db, monkeypatch, *, entry=1.0, scan_fund=1500.0,
                     step_size=0.0001, min_qty=0.0001, max_session_deploy_usd=0.0):
    """Build + start a real pyramid_up session end-to-end (create_pyramid_up_session +
    start_pyramid_up_session), with the exchange info / capital knobs controllable per test."""
    monkeypatch.setattr(market, "get_exchange_info",
                        lambda sym: {"stepSize": step_size, "minQty": min_qty})
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": entry})
    monkeypatch.setattr(settings, "scan_fund", scan_fund)
    monkeypatch.setattr(settings, "pyramid_up_max_adds", 2)
    monkeypatch.setattr(settings, "pyramid_up_step_pct", 2.0)
    monkeypatch.setattr(settings, "pyramid_up_size_ratio", 0.7)
    monkeypatch.setattr(settings, "max_session_deploy_usd", max_session_deploy_usd)
    row = service.create_pyramid_up_session(db, symbol="AAA", entry_price=entry, tp_pct=4.0,
                                            deadline_days=30)
    service.start_pyramid_up_session(db, row.id)
    db.refresh(row)
    return row


def test_defensive_sized_by_base_wave_cost_not_flat_kss_first_wave_usd(db, monkeypatch):
    """N1 — ZBT #570 regression: a shrunk base wave (scan_fund << kss_first_wave_usd=1500 from the
    _cfg fixture) must get a defensive sized proportionally to ITS OWN cost, not the old flat
    kss_first_wave_usd slug (which was 3.2x the base in the real case)."""
    row = _create_started(db, monkeypatch, entry=1.0, scan_fund=300.0)
    base = db.query(KssWave).filter(KssWave.session_id == row.id, KssWave.wave_num == 0).one()
    defw = db.query(KssWave).filter(KssWave.session_id == row.id, KssWave.wave_num == DEF).one()
    base_cost = base.quantity * row.entry_price
    def_cost = defw.quantity * defw.target_price
    assert base_cost < settings.kss_first_wave_usd * 0.5, base_cost  # confirms the shrunk-base setup
    assert def_cost <= base_cost * 1.1, (def_cost, base_cost)       # ~= base, not the flat 1500 slug
    assert def_cost < settings.kss_first_wave_usd * 0.5, def_cost


def test_isolated_fund_includes_defensive_and_total_cost_stays_within_it(db, monkeypatch):
    """N2 — isolated_fund must reserve the defensive's cost too, so a filled base + defensive
    never exceeds the session's own reservation (the ZBT #570 accounting gap: total_cost hit 196%
    of isolated_fund)."""
    row = _create_started(db, monkeypatch, entry=1.0, scan_fund=300.0)
    up_waves = db.query(KssWave).filter(KssWave.session_id == row.id, KssWave.wave_num >= 0).all()
    up_ladder_cost = sum(w.quantity * w.target_price for w in up_waves)
    defw = db.query(KssWave).filter(KssWave.session_id == row.id, KssWave.wave_num == DEF).one()
    def_cost = defw.quantity * defw.target_price
    assert row.isolated_fund >= (up_ladder_cost + def_cost) * 0.99

    base = next(w for w in up_waves if w.wave_num == 0)
    service.handle_fill_event(db, f"pyramid:{row.id}:wave:0", base.quantity, row.entry_price)
    service.handle_fill_event(db, f"pyramid:{row.id}:wave:{DEF}", defw.quantity, defw.target_price)
    db.refresh(row)
    assert row.total_cost <= row.isolated_fund + 1e-6


def test_defensive_sized_to_fit_deploy_cap_headroom(db, monkeypatch):
    """Deploy-cap interaction: with max_session_deploy_usd small, the defensive is shrunk to fit
    the remaining headroom instead of ignoring the per-session cap."""
    row = _create_started(db, monkeypatch, entry=1.0, scan_fund=1500.0,
                          max_session_deploy_usd=50.0)
    base = db.query(KssWave).filter(KssWave.session_id == row.id, KssWave.wave_num == 0).one()
    base_cost = base.quantity * row.entry_price
    assert base_cost > 50.0, base_cost  # the cap actually binds tighter than the base itself
    defw = db.query(KssWave).filter(KssWave.session_id == row.id, KssWave.wave_num == DEF).one()
    def_cost = defw.quantity * defw.target_price
    assert def_cost <= 50.0 * 1.05
    assert def_cost < base_cost


def test_defensive_omitted_when_even_minqty_exceeds_headroom(db, monkeypatch):
    """Edge case: if even a minQty-floored defensive would breach the deploy-cap headroom, the
    rung is OMITTED entirely (ride the hard SL) rather than over-deploying, with an audit trail."""
    row = _create_started(db, monkeypatch, entry=1.0, scan_fund=1500.0,
                          min_qty=1000.0, max_session_deploy_usd=1.0)
    defw = db.query(KssWave).filter(KssWave.session_id == row.id,
                                    KssWave.wave_num == DEF).one_or_none()
    assert defw is None
    log = db.query(AuditLog).filter(
        AuditLog.action == "pyramid_defensive_omitted_no_headroom",
        AuditLog.entity == f"kss:{row.id}",
    ).one_or_none()
    assert log is not None


def test_defensive_uncapped_when_deploy_cap_off(db, monkeypatch):
    """max_session_deploy_usd=0 (off) must not shrink the defensive — headroom is +inf, so sizing
    falls back to pure base-wave-cost matching (no behavior change for the default/off posture)."""
    row = _create_started(db, monkeypatch, entry=1.0, scan_fund=300.0,
                          max_session_deploy_usd=0.0)
    base = db.query(KssWave).filter(KssWave.session_id == row.id, KssWave.wave_num == 0).one()
    defw = db.query(KssWave).filter(KssWave.session_id == row.id, KssWave.wave_num == DEF).one()
    base_cost = base.quantity * row.entry_price
    def_cost = defw.quantity * defw.target_price
    assert def_cost <= base_cost * 1.1


def test_flip_audit_logs_orig_entry(db):
    """(Optional #3) the pre-flip pyramid_up entry is recorded on the flip audit for lossautopsy
    attribution, alongside the new avg the session re-anchors to."""
    import json

    s = _pyr_session(db, base_qty=8000.0, base_px=0.0250)
    orig_entry = s.entry_price
    service.handle_fill_event(db, f"pyramid:{s.id}:wave:{DEF}", 8000.0, 0.0246)
    log = db.query(AuditLog).filter(AuditLog.action == "pyramid_up_flipped",
                                    AuditLog.entity == f"kss:{s.id}").one()
    detail = json.loads(log.detail)
    assert abs(detail["orig_entry"] - orig_entry) < 1e-6
