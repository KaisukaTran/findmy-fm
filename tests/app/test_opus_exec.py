"""O-3 sandbox (policy) + O-4 watch state machine. No network: prices/candidates mocked."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import market, orders, runtime
from app.config import settings
from app.orchestrator import brain, policy, service, watch
from app.orchestrator import models as om


@pytest.fixture
def opus_market(monkeypatch):
    """Patch every price/lot-size/candidate seam so the cage runs fully offline."""
    price = {"BTC": 100.0}

    def prices(syms):
        return {s: price[s] for s in syms if s in price}

    monkeypatch.setattr(market, "get_current_prices", prices)
    monkeypatch.setattr(orders, "get_current_prices", prices)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", prices)
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info",
                        lambda s: {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 1e6})
    monkeypatch.setattr(brain, "_candidates",
                        lambda db, k=25: [{"symbol": "BTC", "decision": "trade",
                                           "consensus": 80, "win_rate": 90, "est_days_to_tp": 3}])
    monkeypatch.setattr(settings, "opus_shadow", False)
    monkeypatch.setattr(settings, "opus_allocation_usd", 2000.0)
    monkeypatch.setattr(settings, "opus_max_trade_notional", 200.0)
    return price


# --- O-3 policy sandbox -------------------------------------------------


def test_shadow_mode_does_not_execute(db, opus_market, monkeypatch):
    monkeypatch.setattr(settings, "opus_shadow", True)
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    assert out["shadow"] is True
    assert db.query(om.OpusPosition).count() == 0


def test_open_clamps_to_cap_and_creates_position(db, opus_market):
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 500, "reason": "x"}])
    assert len(out["executed"]) == 1
    pos = db.query(om.OpusPosition).one()
    assert pos.symbol == "BTC" and pos.state == om.OPUS_WATCH
    # 500 clamped to the 200 cap → qty 2 @ ~100 (fill carries small slippage)
    assert abs(pos.qty - 2.0) < 1e-9
    assert abs(service.deployed(db) - 200.0) < 1.0  # slippage ≈ 0.05%


def test_open_rejects_unknown_symbol(db, opus_market):
    out = policy.apply_intents(db, [{"action": "open", "symbol": "ETH", "notional": 100}])
    assert out["executed"] == [] and out["rejected"][0]["reason"].startswith("symbol")


def test_open_rejects_dust(db, opus_market, monkeypatch):
    monkeypatch.setattr(settings, "scan_min_notional", 10.0)
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 3}])
    assert out["executed"] == [] and db.query(om.OpusPosition).count() == 0


def test_frozen_blocks_all(db, opus_market):
    runtime.freeze(db, "test")
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    assert out["executed"] == [] and out["rejected"][0]["reason"] == "frozen"


def test_close_realizes_and_marks_closed(db, opus_market):
    policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    pos = db.query(om.OpusPosition).one()
    out = policy.apply_intents(db, [{"action": "close", "position_id": pos.id}])
    assert len(out["executed"]) == 1
    db.refresh(pos)
    assert pos.state == om.OPUS_CLOSED and pos.closed_at is not None


# --- O-4 watch state machine -------------------------------------------


def _watch_pos(db, *, qty, avg, hours_ago):
    pos = om.OpusPosition(symbol="BTC", state=om.OPUS_WATCH, qty=qty, avg_price=avg,
                          entry_price=avg, opened_at=datetime.utcnow() - timedelta(hours=hours_ago),
                          watch_started_at=datetime.utcnow() - timedelta(hours=hours_ago))
    db.add(pos)
    db.commit()
    return pos


def test_watch_winner_becomes_ride(db, opus_market):
    pos = _watch_pos(db, qty=1.0, avg=90.0, hours_ago=4)  # price 100 > avg 90 → winner
    watch.run(db)
    db.refresh(pos)
    assert pos.state == om.OPUS_RIDE


def test_watch_loser_rescued_into_kss(db, opus_market):
    pos = _watch_pos(db, qty=1.0, avg=120.0, hours_ago=4)  # price 100 < avg 120 → loser
    watch.run(db)
    db.refresh(pos)
    assert pos.state == om.OPUS_RESCUE
    assert pos.kss_session_id is not None
    from app.models import KssSession
    sess = db.get(KssSession, pos.kss_session_id)
    assert sess is not None and sess.status == "active"


def test_watch_young_position_untouched(db, opus_market):
    pos = _watch_pos(db, qty=1.0, avg=90.0, hours_ago=1)  # < 3h
    watch.run(db)
    db.refresh(pos)
    assert pos.state == om.OPUS_WATCH


def test_ride_hard_stop_closes(db, opus_market, monkeypatch):
    monkeypatch.setattr(settings, "opus_ride_hard_sl_pct", 10.0)  # stop at avg*0.9
    pos = om.OpusPosition(symbol="BTC", state=om.OPUS_RIDE, qty=1.0, avg_price=120.0,
                          entry_price=120.0, opened_at=datetime.utcnow() - timedelta(hours=5))
    db.add(pos)
    db.commit()
    # price 100 ≤ 120*0.9=108 → hard stop fires
    watch.run(db)
    db.refresh(pos)
    assert pos.state == om.OPUS_CLOSED


# --- K-1: one owner per coin (no duplicate lots / sessions) -------------


def test_open_rejected_when_opus_already_holds(db, opus_market):
    """Don't stack a second OPUS lot on a coin we already hold (root of duplicate sessions)."""
    policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    assert out["executed"] == []
    assert "OPUS already holds" in out["rejected"][0]["reason"]
    assert db.query(om.OpusPosition).count() == 1


# --- P2/O-LIVE: cage-side consensus floor + daily-loss stop ------------


def _seed_candidate(db, symbol: str, consensus_pct: float):
    """One ScanRun + Candidate row so `_candidate_consensus` (the floor's data source) has
    something real to look up, independent of `opus_market`'s `brain._candidates` mock."""
    from app.models import Candidate, ScanRun
    scan = ScanRun()
    db.add(scan)
    db.flush()
    db.add(Candidate(scan_id=scan.id, symbol=symbol, consensus_pct=consensus_pct, decision="trade"))
    db.commit()


def test_open_floor_rejects_below_consensus(db, opus_market, monkeypatch):
    """P2: the deterministic consensus floor is cage-side — enforced regardless of who
    proposed the open, against the SAME latest-scan Candidate row the candidate gate uses."""
    _seed_candidate(db, "BTC", consensus_pct=55.0)
    monkeypatch.setattr(settings, "opus_solo_min_consensus", 70.0)
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    assert out["executed"] == []
    reason = out["rejected"][0]["reason"]
    assert "consensus" in reason and "floor" in reason
    assert db.query(om.OpusPosition).count() == 0


def test_open_floor_zero_passes(db, opus_market, monkeypatch):
    """Floor 0 (the P2 trial value) never rejects, even against a low-consensus candidate."""
    _seed_candidate(db, "BTC", consensus_pct=55.0)
    monkeypatch.setattr(settings, "opus_solo_min_consensus", 0.0)
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    assert len(out["executed"]) == 1


def test_open_floor_does_not_preempt_candidate_gate(db, opus_market, monkeypatch):
    """A symbol the scanner never surfaced is rejected by the candidate whitelist FIRST,
    before the consensus floor is even evaluated (floor set permissive here on purpose)."""
    monkeypatch.setattr(settings, "opus_solo_min_consensus", 0.0)
    out = policy.apply_intents(db, [{"action": "open", "symbol": "ETH", "notional": 100}])
    assert out["executed"] == []
    assert out["rejected"][0]["reason"].startswith("symbol not a current candidate")


def test_daily_loss_stop_blocks_opens_but_closes_still_execute(db, opus_market, monkeypatch):
    """Once today's net breaches -X% of the allocation, new opens are rejected but an
    existing position can still be closed (risk reduction never waits for the brake)."""
    monkeypatch.setattr(settings, "opus_daily_loss_stop_pct", 3.0)
    monkeypatch.setattr(settings, "opus_allocation_usd", 1000.0)
    db.add(om.OpusMetricHourly(hour_ts=datetime.utcnow(), net_pnl=-50.0))  # -5% breaches -3%
    db.commit()
    pos = _watch_pos(db, qty=1.0, avg=90.0, hours_ago=1)

    out = policy.apply_intents(db, [
        {"action": "open", "symbol": "BTC", "notional": 100},
        {"action": "close", "position_id": pos.id},
    ])
    opens = [e for e in out["executed"] if e["action"] == "open"]
    closes = [e for e in out["executed"] if e["action"] == "close"]
    assert opens == []
    assert len(closes) == 1
    assert any(r["reason"] == "daily_loss_stop" for r in out["rejected"])


def test_daily_loss_stop_inactive_above_the_line(db, opus_market, monkeypatch):
    monkeypatch.setattr(settings, "opus_daily_loss_stop_pct", 3.0)
    monkeypatch.setattr(settings, "opus_allocation_usd", 1000.0)
    db.add(om.OpusMetricHourly(hour_ts=datetime.utcnow(), net_pnl=-10.0))  # only -1%
    db.commit()
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    assert len(out["executed"]) == 1


# --- P3: 'reduce' intent (partial take-profit) --------------------------


def test_reduce_partial_sells_and_updates_qty_and_realized(db, opus_market):
    policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 200, "reason": "x"}])
    pos = db.query(om.OpusPosition).one()
    assert abs(pos.qty - 2.0) < 1e-6  # 200 @ 100

    out = policy.apply_intents(
        db, [{"action": "reduce", "position_id": pos.id, "notional": 100, "reason": "bank"}]
    )
    assert len(out["executed"]) == 1
    assert out["executed"][0]["action"] == "reduce"
    db.refresh(pos)
    assert abs(pos.qty - 1.0) < 1e-6          # sold half (100 / 100)
    assert pos.state == om.OPUS_WATCH         # a reduce never changes state

    from app.models import Fill
    fill = db.query(Fill).filter(Fill.source_ref == f"opus:{pos.id}:reduce").one()
    assert fill.side == "SELL"
    assert abs(pos.realized_pnl - (fill.realized_pnl or 0.0)) < 1e-9


def test_reduce_rejects_unknown_position(db, opus_market):
    out = policy.apply_intents(
        db, [{"action": "reduce", "position_id": 999, "notional": 50, "reason": "bank"}]
    )
    assert out["executed"] == []
    assert out["rejected"][0]["reason"].startswith("position not open")


def test_reduce_escalates_to_close_when_remainder_is_dust(db, opus_market, monkeypatch):
    """A reduce that would leave a tail below min notional never strands it — the whole
    position closes instead."""
    monkeypatch.setattr(settings, "scan_min_notional", 50.0)
    policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 200, "reason": "x"}])
    pos = db.query(om.OpusPosition).one()

    out = policy.apply_intents(
        db, [{"action": "reduce", "position_id": pos.id, "notional": 180, "reason": "bank"}]
    )
    assert len(out["executed"]) == 1
    assert out["executed"][0]["action"] == "close"
    db.refresh(pos)
    assert pos.state == om.OPUS_CLOSED


def test_reduce_allowed_while_daily_loss_stop_blocks_open(db, opus_market, monkeypatch):
    """Risk reduction (reduce) is never gated by the daily-loss brake — only new opens are."""
    policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100, "reason": "x"}])
    pos = db.query(om.OpusPosition).one()

    monkeypatch.setattr(settings, "opus_daily_loss_stop_pct", 3.0)
    monkeypatch.setattr(settings, "opus_allocation_usd", 1000.0)
    db.add(om.OpusMetricHourly(hour_ts=datetime.utcnow(), net_pnl=-50.0))  # -5% breaches -3%
    db.commit()

    out = policy.apply_intents(db, [
        {"action": "open", "symbol": "BTC", "notional": 50, "reason": "y"},
        {"action": "reduce", "position_id": pos.id, "notional": 30, "reason": "bank"},
    ])
    opens = [e for e in out["executed"] if e["action"] == "open"]
    reduces = [e for e in out["executed"] if e["action"] == "reduce"]
    assert opens == []
    assert len(reduces) == 1
    assert any(r["reason"] == "daily_loss_stop" for r in out["rejected"])


def test_daily_loss_stop_knob_zero_always_inactive(db, opus_market, monkeypatch):
    monkeypatch.setattr(settings, "opus_daily_loss_stop_pct", 0.0)
    monkeypatch.setattr(settings, "opus_allocation_usd", 1000.0)
    db.add(om.OpusMetricHourly(hour_ts=datetime.utcnow(), net_pnl=-500.0))  # huge loss
    db.commit()
    assert service.daily_loss_stop_active(db) is False
    out = policy.apply_intents(db, [{"action": "open", "symbol": "BTC", "notional": 100}])
    assert len(out["executed"]) == 1


def test_second_rescue_merges_into_existing_session(db, opus_market):
    """Two losing lots on one coin → ONE KSS session owning the combined qty (K-1), not two."""
    from app.models import SESSION_ACTIVE, KssSession

    p1 = _watch_pos(db, qty=1.0, avg=120.0, hours_ago=4)  # price 100 < avg → loser
    p2 = _watch_pos(db, qty=2.0, avg=130.0, hours_ago=4)  # loser
    watch.run(db)
    db.refresh(p1)
    db.refresh(p2)

    sessions = (
        db.query(KssSession)
        .filter(KssSession.symbol == "BTC", KssSession.status == SESSION_ACTIVE)
        .all()
    )
    assert len(sessions) == 1  # merged, not duplicated
    assert p1.kss_session_id == sessions[0].id
    assert p2.kss_session_id == sessions[0].id
    assert abs(sessions[0].total_filled_qty - 3.0) < 1e-9  # 1.0 + 2.0 folded in
