"""Runner mode (``kss_arm_at_tp``): the session's own take-profit ARMS the trail instead of selling.

The XPL case this exists for: a session sold at its fixed TP (+6.2%) and the coin ran on to +22.6%.
The headline guarantees, each locked by a test below:
  - below TP nothing changes (no arm, no sell) — so the mode adds no path that ends in a loss;
  - once armed, every automatic exit sits at or above the lock floor (> fee floor) — never a loss;
  - a steady runner on a high-ATR coin is NOT sold by the spike-grab ceiling (it was, at ~+7%);
  - on live maker, no fixed TP rests on the exchange ahead of the trail.
Prices + ATR are monkeypatched (no network).
"""

from __future__ import annotations

import random

import pytest

from app import execution, market
from app.config import settings
from app.kss import dynamic_exit as dx
from app.kss import service
from app.models import (
    PENDING,
    REJECTED,
    SESSION_ACTIVE,
    SESSION_STOPPED,
    KssSession,
    PendingOrder,
)

ATR = 8.0  # a high-ATR alt: the trail (8%) is wider than the 5% spike-grab gap


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "taker_fee_pct", 0.1)
    monkeypatch.setattr(settings, "slippage_pct", 0.05)          # round-trip cost 0.3% → floor +0.9%
    monkeypatch.setattr(settings, "binance_max_fee_pct", 0.1)
    monkeypatch.setattr(settings, "tp_fee_coverage", 1.2)        # TP = tp_pct + 0.24%
    monkeypatch.setattr(settings, "kss_exit_fee_mult", 3.0)
    monkeypatch.setattr(settings, "kss_tp_gap_pct", 5.0)
    monkeypatch.setattr(settings, "kss_trail_atr_mult", 1.0)
    monkeypatch.setattr(settings, "kss_trail_min_pct", 3.0)
    monkeypatch.setattr(settings, "kss_trail_arm_pct", 5.0)
    monkeypatch.setattr(settings, "kss_trail_lock_pct", 2.0)
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", True)
    monkeypatch.setattr(settings, "kss_arm_at_tp", True)
    monkeypatch.setattr(settings, "kss_trail_lock_tp_ratio", 0.5)
    monkeypatch.setattr(settings, "kss_trail_keep_pct", 60.0)
    monkeypatch.setattr(service, "_session_atr_pct", lambda sym: ATR)


def _price(monkeypatch, px):
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": px})


def _session(db, **kw):
    d = {"symbol": "AAA", "entry_price": 100.0, "distance_pct": 1.5, "max_waves": 6,
         "isolated_fund": 1000.0, "tp_pct": 6.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0,
         "status": SESSION_ACTIVE, "current_wave": 2, "avg_price": 100.0, "total_filled_qty": 10.0,
         "total_cost": 1000.0, "peak_price": 0.0, "sl_pct": 8.0}
    s = KssSession(**(d | kw))
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _exits(db, sid):
    return db.query(PendingOrder).filter(
        PendingOrder.source_ref.like(f"pyramid:{sid}:%"), PendingOrder.side == "SELL").all()


def _tp(row) -> float:
    return service._to_pyramid(row).estimated_tp_price     # 106.24 with the fixture fees


# ----- pure math -----

def test_mode_off_keeps_ride_and_trail(monkeypatch):
    monkeypatch.setattr(settings, "kss_arm_at_tp", False)
    assert dx.arm_threshold(100.0, tp_price=106.24) == pytest.approx(105.0)   # +arm_pct
    assert dx.lock_floor_price(100.0, tp_price=106.24) == pytest.approx(102.0)
    assert dx.keep_floor_price(peak=120.0, avg=100.0, tp_price=106.24) == 0.0


def test_arm_threshold_is_the_session_tp():
    assert dx.arm_threshold(100.0, tp_price=106.24) == pytest.approx(106.24)
    assert dx.arm_threshold(100.0) == pytest.approx(105.0)      # no TP given → Ride & Trail


def test_lock_floor_keeps_a_share_of_the_tp_gain():
    assert dx.lock_floor_price(100.0, tp_price=106.24) == pytest.approx(103.12)   # half of +6.24%


def test_lock_floor_never_below_fee_floor(monkeypatch):
    monkeypatch.setattr(settings, "kss_trail_lock_tp_ratio", 0.0)
    assert dx.lock_floor_price(100.0, tp_price=106.24) == pytest.approx(dx.fee_floor_price(100.0))


def test_keep_floor_holds_a_share_of_the_peak_gain():
    assert dx.keep_floor_price(peak=122.6, avg=100.0, tp_price=106.24) == pytest.approx(113.56)
    sl = dx.compute_sl(peak=122.6, avg=100.0, distance_pct=1.5, trail_dist_pct=ATR, tp_price=106.24)
    assert sl >= 113.56 - 1e-6


def test_ceiling_is_anchored_to_the_peak():
    # SL-anchored the ceiling would be 103.12×1.05 = 108.28 — below a 110 peak, i.e. an instant sell.
    tp = dx.compute_tp(sl=103.12, avg=100.0, peak=110.0, tp_price=106.24)
    assert tp == pytest.approx(115.5)                           # peak×(1+gap)


def test_legacy_ceiling_unchanged_when_mode_off(monkeypatch):
    monkeypatch.setattr(settings, "kss_arm_at_tp", False)
    assert dx.compute_tp(sl=103.12, avg=100.0, peak=110.0, tp_price=106.24) == pytest.approx(108.276)


def _walk(path, *, avg=100.0, tp_price=106.24, d=1.5):
    """Drive the pure channel along a price path exactly like _evaluate_dynamic_exit (check the
    carried edges, then ratchet). Returns (exit_kind, exit_price, lock_floor) or None."""
    td = dx.trail_distance_pct(ATR)
    armed, sl, peak = False, 0.0, 0.0
    for p in path:
        if not armed:
            if dx.should_arm(market=p, avg=avg, filled_qty=1.0, trail_active=False, tp_price=tp_price):
                armed, peak = True, p
                sl = dx.compute_sl(peak=p, avg=avg, distance_pct=d, trail_dist_pct=td, tp_price=tp_price)
                assert sl < p                                   # the armed stop sits below the price
            continue
        if p >= dx.compute_tp(sl=sl, avg=avg, peak=peak, tp_price=tp_price):
            return "tp", p, dx.lock_floor_price(avg, tp_price)
        if p <= sl:
            return "trail_sl", sl, dx.lock_floor_price(avg, tp_price)
        peak = max(peak, p)
        sl = dx.compute_sl(peak=peak, avg=avg, distance_pct=d, trail_dist_pct=td, prev_sl=sl,
                           tp_price=tp_price)
    return None


def test_xpl_runner_rides_past_tp_and_keeps_most_of_the_run():
    up = [106.24 * 1.005 ** i for i in range(0, 300) if 106.24 * 1.005 ** i < 122.6] + [122.6]
    down = [122.6 * 0.99 ** i for i in range(1, 40)]
    kind, level, _ = _walk(up + down)
    assert kind == "trail_sl"
    assert level >= 113.56 - 1e-6        # ≥ 60% of the +22.6% peak gain — vs +6.24% at fixed TP


def test_every_exit_after_arming_is_a_profit():
    """Property: along random paths, an armed session only ever exits at or above its lock floor
    (and so above the fee floor) — no automatic exit books a loss."""
    rng = random.Random(7)
    for _ in range(300):
        p, path = 100.0, []
        for _ in range(400):
            p *= 1 + rng.gauss(0.001, 0.02)
            path.append(p)
        out = _walk(path)
        if out is None:
            continue
        _, level, floor = out
        assert level >= floor - 1e-6 > dx.fee_floor_price(100.0)


# ----- wiring (manage_open_sessions) -----

def test_below_tp_does_not_arm_or_sell(db, monkeypatch):
    s = _session(db)
    _price(monkeypatch, 105.5)            # > Ride & Trail's +5% arm, still < the 106.24 TP
    service.manage_open_sessions(db)
    db.refresh(s)
    assert s.trail_active is False and s.status == SESSION_ACTIVE
    assert _exits(db, s.id) == []


def test_tp_arms_instead_of_selling(db, monkeypatch):
    s = _session(db)
    _price(monkeypatch, 106.5)            # ≥ TP
    service.manage_open_sessions(db)
    db.refresh(s)
    assert s.trail_active is True and s.status == SESSION_ACTIVE
    assert _exits(db, s.id) == []         # the fixed TP did NOT sell
    assert dx.lock_floor_price(100.0, _tp(s)) - 1e-6 <= s.trail_sl_price < 106.5


def test_high_atr_runner_is_not_capped_then_trails_out_in_profit(db, monkeypatch):
    s = _session(db)
    for px in (106.5, 109.0, 112.0, 116.0, 120.0, 122.6):    # steady climb, no exit
        _price(monkeypatch, px)
        service.manage_open_sessions(db)
        db.refresh(s)
        assert s.status == SESSION_ACTIVE and _exits(db, s.id) == [], px
    sl = s.trail_sl_price
    assert sl >= 113.56 - 1e-6
    _price(monkeypatch, sl - 0.1)                             # reversal through the stop
    service.manage_open_sessions(db)
    db.refresh(s)
    assert s.status == SESSION_STOPPED
    assert [o.source_ref for o in _exits(db, s.id)] == [f"pyramid:{s.id}:trail_sl"]


def test_mode_off_keeps_ride_and_trail_arm(db, monkeypatch):
    monkeypatch.setattr(settings, "kss_arm_at_tp", False)
    s = _session(db)
    _price(monkeypatch, 105.5)            # ≥ +5% → Ride & Trail arms below the TP
    service.manage_open_sessions(db)
    db.refresh(s)
    assert s.trail_active is True


def test_pyramid_up_is_not_in_runner_mode(db):
    s = _session(db, strategy_mode="pyramid_up")
    assert service._runner_tp_price(s) == 0.0


def test_runner_knobs_round_trip(db, monkeypatch):
    from app import runtime
    runtime.set_kss_settings(db, {"kss_arm_at_tp": "0", "kss_trail_lock_tp_ratio": "0.4",
                                  "kss_trail_keep_pct": "70"})
    k = runtime.kss_settings(db)
    assert k["kss_arm_at_tp"] is False
    assert k["kss_trail_lock_tp_ratio"] == 0.4
    assert k["kss_trail_keep_pct"] == 70.0


# ----- live maker: no fixed TP resting ahead of the trail -----

class _StubProvider:
    def pair(self, symbol):
        return f"{symbol}/USDT"


def _live_maker(monkeypatch):
    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _StubProvider())
    monkeypatch.setattr(execution, "fetch_live_order", lambda pair, oid: {
        "status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0, "raw_id": oid,
    })
    monkeypatch.setattr(settings, "maker_orders", True)
    monkeypatch.setattr(settings, "auto_trade", True)


def _resting_tp(db, sid):
    return db.query(PendingOrder).filter(PendingOrder.source_ref == f"pyramid:{sid}:tp").all()


def test_no_resting_tp_when_the_trail_governs(db, monkeypatch):
    _live_maker(monkeypatch)
    s = _session(db)
    assert service.sync_resting_tp(db)["queued"] == 0
    assert _resting_tp(db, s.id) == []


def test_resting_tp_already_on_the_book_is_taken_off(db, monkeypatch):
    _live_maker(monkeypatch)
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", False)
    s = _session(db)
    service.sync_resting_tp(db)                               # dynamic off → the TP rests
    assert [o.status for o in _resting_tp(db, s.id)] == [PENDING]
    monkeypatch.setattr(settings, "kss_dynamic_tp_enabled", True)
    assert service.sync_resting_tp(db)["dropped"] == 1        # flag on → taken off the book
    assert [o.status for o in _resting_tp(db, s.id)] == [REJECTED]


def test_dynamic_market_tp_exit_is_not_mistaken_for_a_resting_tp(db, monkeypatch):
    """A spike-grab exit shares the ``:tp`` ref but is a MARKET sell of a closed session. Treating it
    as a stale resting TP would reject the very order that closes the position."""
    _live_maker(monkeypatch)
    s = _session(db, status="tp_triggered")
    db.add(PendingOrder(symbol="AAA", side="SELL", quantity=10.0, price=0.0, order_type="MARKET",
                        status=PENDING, source="kss", source_ref=f"pyramid:{s.id}:tp"))
    db.commit()
    assert service.sync_resting_tp(db)["dropped"] == 0
    assert [o.status for o in _resting_tp(db, s.id)] == [PENDING]
