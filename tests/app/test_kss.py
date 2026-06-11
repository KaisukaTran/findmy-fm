"""
KSS tests for the lean rebuild.

Covers the preserved PyramidSession math, the linear preview projection, and a
DB-backed integration flow (create -> start -> approve fill -> auto-queue next wave)
that exercises app.kss.service + app.orders together.
"""

import pytest

from app import models, orders
from app.kss import service
from app.kss.pyramid import PyramidSession, PyramidSessionStatus, WaveInfo

_EX_INFO = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}


@pytest.fixture
def mock_market(monkeypatch):
    """Mock exchange info + prices used inside the strategy module."""
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: _EX_INFO)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {"BTC": 49000.0})


# --- PyramidSession math (preserved logic) ------------------------------


class TestPyramid:
    @pytest.fixture
    def session(self, mock_market):
        return PyramidSession(
            symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=10,
            isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
        )

    def test_init_valid(self, session):
        assert session.status == PyramidSessionStatus.PENDING
        assert session.entry_price == 50000.0

    def test_init_invalid_entry(self, mock_market):
        with pytest.raises(ValueError, match="Entry price must be positive"):
            PyramidSession("BTC", -1, 2.0, 10, 1000.0, 3.0, 30.0, 5.0)

    def test_init_invalid_distance(self, mock_market):
        with pytest.raises(ValueError, match="Distance must be"):
            PyramidSession("BTC", 50000.0, 150.0, 10, 1000.0, 3.0, 30.0, 5.0)

    def test_generate_wave_0_and_1(self, session):
        w0 = session.generate_wave(0)
        w1 = session.generate_wave(1)
        assert w0.wave_num == 0 and w0.target_price == 50000.0
        assert w1.quantity > w0.quantity
        assert abs(w1.target_price - 50000.0 * 0.98) < 0.01

    def test_generate_wave_5_geometric(self, session):
        w5 = session.generate_wave(5)
        expected = 50000.0 * (0.98 ** 5)
        assert abs(w5.target_price - expected) < 1.0

    def test_start_then_fill_next_wave(self, session):
        order = session.start()
        assert order["side"] == "BUY" and session.status == PyramidSessionStatus.ACTIVE
        res = session.on_fill(0, 0.00002, 50000.0, current_market_price=49000.0)
        assert res["action"] == "next_wave"
        assert abs(session.avg_price - 50000.0) < 0.01
        assert len(session.waves) == 2

    def test_tp_triggers(self, session):
        session.start()
        session.on_fill(0, 0.00002, 50000.0, current_market_price=50000.0)
        res = session.check_tp(52000.0)  # > 50000 * 1.03
        assert res["action"] == "tp_triggered"
        assert res["order"]["side"] == "SELL"
        assert session.status == PyramidSessionStatus.TP_TRIGGERED

    def test_tp_not_triggered(self, session):
        session.start()
        session.on_fill(0, 0.00002, 50000.0, current_market_price=50000.0)
        assert session.check_tp(50500.0) is None
        assert session.status == PyramidSessionStatus.ACTIVE

    def test_adjust_and_stop(self, session):
        assert "tp_pct" in session.adjust_params(tp_pct=5.0)
        session.current_wave = 5
        assert "max_waves" not in session.adjust_params(max_waves=3)
        session.status = PyramidSessionStatus.ACTIVE
        session.stop()
        assert session.status == PyramidSessionStatus.STOPPED

    def test_estimated_tp_price(self, session):
        assert abs(session.estimated_tp_price - 50000.0 * 1.03) < 0.01
        session.avg_price = 48000.0
        assert abs(session.estimated_tp_price - 48000.0 * 1.03) < 0.01


def test_waveinfo_to_dict():
    from datetime import datetime

    d = WaveInfo(3, 0.00008, 48000.0, "filled", 0.00008, 47950.0,
                 datetime(2026, 1, 12), 42).to_dict()
    assert d["wave_num"] == 3 and d["pending_order_id"] == 42 and "2026-01-12" in d["filled_time"]


# --- preview projection (linear, equal qty) -----------------------------


class TestPreview:
    def test_basic(self):
        r = service.preview("BTC", 50000.0, 2.0, 5, 1000.0, 3.0)
        assert len(r["waves"]) == 5
        assert r["waves"][0]["target_price"] == 50000.0
        assert abs(r["waves"][4]["target_price"] - 50000.0 * (1 - 0.02 * 4)) < 0.01

    def test_qty_and_totals(self):
        r = service.preview("ETH", 2000.0, 5.0, 4, 800.0, 2.0)
        assert abs(r["qty_per_wave"] - 0.1) < 1e-4
        assert abs(r["total_qty"] - 0.4) < 1e-4

    def test_running_averages(self):
        r = service.preview("BTC", 100.0, 10.0, 3, 300.0, 5.0)
        assert r["waves"][0]["avg_price_after"] == 100.0
        assert abs(r["waves"][1]["avg_price_after"] - 95.0) < 0.01
        assert abs(r["waves"][2]["avg_price_after"] - 90.0) < 0.01

    def test_tp_prices(self):
        r = service.preview("BTC", 100.0, 10.0, 2, 200.0, 10.0)
        assert abs(r["waves"][0]["tp_price_after"] - 110.0) < 0.01
        assert abs(r["waves"][1]["tp_price_after"] - 104.5) < 0.01

    def test_price_range(self):
        r = service.preview("BTC", 100.0, 5.0, 5, 500.0, 3.0)
        assert abs(r["price_range_pct"] - 20.0) < 0.01

    def test_single_wave(self):
        r = service.preview("ETH", 3000.0, 1.0, 1, 100.0, 5.0)
        assert len(r["waves"]) == 1 and r["price_range_pct"] == 0.0


# --- DB-backed service integration --------------------------------------


def test_service_flow_create_start_fill(db, mock_market):
    row = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=3,
        isolated_fund=100000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    assert row.status == models.SESSION_PENDING

    res = service.start_session(db, row.id)
    poid = res["pending_order_id"]
    assert db.query(models.KssWave).filter_by(session_id=row.id).count() == 1

    # Approving the wave-0 order fires the KSS hook, which queues wave 1.
    fill = orders.approve_order(db, poid)
    assert fill.realized_pnl == 0.0  # BUY

    db.refresh(row)
    assert row.status == models.SESSION_ACTIVE
    assert row.total_filled_qty > 0

    waves = db.query(models.KssWave).filter_by(session_id=row.id).all()
    assert len(waves) == 2  # wave 0 (filled) + wave 1 (sent)
    assert any(w.status == models.WAVE_FILLED for w in waves)

    pend = orders.list_pending(db)
    assert any(p.source_ref == f"pyramid:{row.id}:wave:1" for p in pend)


def test_queue_next_wave_after_extending_ladder(db, mock_market):
    """A dormant ladder (all waves filled, max reached) resumes once max_waves is raised."""
    row = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=2,
        isolated_fund=100000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])  # fill wave 0 → auto-queues wave 1
    w1 = next(p for p in orders.list_pending(db) if p.source_ref == f"pyramid:{row.id}:wave:1")
    orders.approve_order(db, w1.id)  # fill wave 1 → ladder full (max_waves=2), nothing queued

    # Exhausted: manual DCA must refuse until the ladder is extended.
    with pytest.raises(ValueError, match="Ladder exhausted"):
        service.queue_next_wave(db, row.id)

    service.adjust_session(db, row.id, max_waves=4)
    out = service.queue_next_wave(db, row.id)
    assert out["wave_num"] == 2
    pend = orders.list_pending(db)
    assert any(p.source_ref == f"pyramid:{row.id}:wave:2" for p in pend)
    db.refresh(row)
    assert row.current_wave == 2


def test_consolidate_sessions_merges_into_one_owner(db, mock_market):
    """Two active sessions on one coin → keeper owns the whole Position; the other is removed."""
    from app.orchestrator import models as om

    keep = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=6,
        isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    drop = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=6,
        isolated_fund=1500.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    db.add(models.Position(symbol="BTC", quantity=3.0, avg_entry_price=48000.0, total_cost=144000.0))
    db.add(om.OpusPosition(symbol="BTC", state=om.OPUS_RESCUE, qty=1.0, avg_price=48000.0,
                           kss_session_id=drop.id))
    db.commit()

    out = service.consolidate_sessions(db, keep_id=keep.id, merge_id=drop.id)
    assert out["total_filled_qty"] == 3.0 and out["avg_price"] == 48000.0
    db.refresh(keep)
    assert keep.total_filled_qty == 3.0 and keep.avg_price == 48000.0
    assert keep.isolated_fund == 2500.0  # 1000 + 1500
    assert db.get(models.KssSession, drop.id) is None  # merged session removed
    opos = db.query(om.OpusPosition).one()
    assert opos.kss_session_id == keep.id  # rescue link repointed


def test_service_delete_and_summary(db, mock_market):
    row = service.create_session(
        db, symbol="ETH", entry_price=3000.0, distance_pct=1.5, max_waves=5,
        isolated_fund=500.0, tp_pct=2.5, timeout_x_min=20.0, gap_y_min=3.0,
    )
    s = service.summary(db)
    assert s["total_sessions"] == 1
    service.delete_session(db, row.id)
    assert service.summary(db)["total_sessions"] == 0
