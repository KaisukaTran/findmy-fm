"""
KSS tests for the lean rebuild.

Covers the preserved PyramidSession math, the linear preview projection, and a
DB-backed integration flow (create -> start -> approve fill -> auto-queue next wave)
that exercises app.kss.service + app.orders together.
"""

import pytest

from app import models, orders
from app.config import settings
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
        from app import costengine
        eff = 1 + (3.0 + costengine.tp_fee_buffer_pct()) / 100  # tp_pct + fee buffer
        assert abs(session.estimated_tp_price - 50000.0 * eff) < 0.01
        session.avg_price = 48000.0
        assert abs(session.estimated_tp_price - 48000.0 * eff) < 0.01


def test_tp_always_adds_120pct_of_round_trip_fee(mock_market):
    """User rule: every TP target adds 120% of the round-trip fee (buy + sell) on top of
    tp_pct, so a take-profit always clears its fees with a margin (paper AND live)."""
    from app import costengine

    s = PyramidSession(symbol="BTC", entry_price=100.0, distance_pct=2.0, max_waves=5,
                       isolated_fund=1000.0, tp_pct=4.0, timeout_x_min=30.0, gap_y_min=5.0)
    s.avg_price = 100.0
    s.total_filled_qty = 1.0

    buffer = costengine.tp_fee_buffer_pct()
    assert buffer == pytest.approx(1.2 * 2 * settings.binance_max_fee_pct)  # 120% of buy+sell fee
    target = 100.0 * (1 + (4.0 + buffer) / 100)                            # avg × (1 + tp% + buffer)
    assert s.estimated_tp_price == pytest.approx(target)
    assert s.check_tp(target - 0.001) is None                             # below → no TP
    res = s.check_tp(target)                                              # at/above → TP
    assert res is not None and res["action"] == "tp_triggered"


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


def test_preview_next_wave_suggests_cost_even_when_ladder_full(db, mock_market):
    """preview_next_wave is read-only and returns the next rung's $ even on a FULL ladder
    (where a plain DCA+ refuses) — so the UI can suggest a deliberate DCA+ amount."""
    row = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=2,
        isolated_fund=100000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])  # fill wave 0 → queues wave 1
    w1 = next(p for p in orders.list_pending(db) if p.source_ref == f"pyramid:{row.id}:wave:1")
    orders.approve_order(db, w1.id)  # fill wave 1 → ladder full (max_waves=2)

    pv = service.preview_next_wave(db, row.id)
    assert pv["wave_num"] == 2
    assert pv["ladder_full"] is True
    assert pv["cost"] > 0 and pv["quantity"] > 0 and pv["price"] > 0
    assert pv["idle_deployable"] >= 0

    # Read-only: the preview must NOT mutate state — a plain DCA+ still refuses,
    # and no extra wave row was created.
    with pytest.raises(ValueError, match="Ladder exhausted"):
        service.queue_next_wave(db, row.id)
    assert db.query(models.KssWave).filter_by(session_id=row.id).count() == 2


def test_dca_next_funds_from_idle_cash_when_reservation_exhausted(db, mock_market, monkeypatch):
    """Manual DCA+ deploys idle account cash when the session's own isolated_fund is used up
    (the reservation is a planning cap, not real set-aside cash)."""
    row = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=6,
        isolated_fund=100000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])  # fill wave 0
    db.refresh(row)
    row.isolated_fund = row.total_cost  # exhaust the reservation → remaining_fund = 0
    db.commit()
    monkeypatch.setattr(settings, "account_equity", 1_000_000.0)  # plenty of free cash
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"BTC": 100000.0})

    out = service.queue_next_wave(db, row.id)
    assert out["wave_num"] == 2
    pend = orders.list_pending(db)
    assert any(p.source_ref == f"pyramid:{row.id}:wave:2" for p in pend)
    db.refresh(row)
    assert row.isolated_fund > row.total_cost  # reservation grew to fund the wave from idle cash
    assert db.query(models.AuditLog).filter_by(action="dca_fund_topup").count() == 1


def test_dca_next_blocked_when_no_idle_cash(db, mock_market, monkeypatch):
    """DCA+ still refuses when neither the reservation nor idle cash can fund the wave."""
    row = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=6,
        isolated_fund=100000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])
    db.refresh(row)
    row.isolated_fund = row.total_cost
    db.commit()
    # Starting capital ≈ already deployed → free cash ~0 → no idle to fund the wave.
    monkeypatch.setattr(settings, "account_equity", 1.0)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"BTC": 100000.0})
    with pytest.raises(ValueError, match="tiền nhàn rỗi"):
        service.queue_next_wave(db, row.id)


def test_dca_next_anchors_rung_below_market_not_above(db, mock_market, monkeypatch):
    """A DCA+ rung is re-anchored BELOW the live market by the step %, never above it — an
    entry-anchored geometric rung drifts above price after a fast drop and would overpay."""
    monkeypatch.setattr(settings, "sl_pct", 0.0)  # isolate the anchoring from the SL floor
    row = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=10,
        isolated_fund=100000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])  # fill wave 0 → auto-queues wave 1
    # Price has crashed far below the entry-anchored ladder (geometric wave 2 ≈ 48020).
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"BTC": 40000.0})

    out = service.queue_next_wave(db, row.id)
    assert out["price"] < 40000.0                      # below the live market
    assert abs(out["price"] - 40000.0 * 0.98) < 1.0    # exactly the step % below market


def test_dca_next_custom_amount_deploys_chosen_usd(db, mock_market, monkeypatch):
    """Manual DCA+ with amount_usd deploys exactly that USD slice (qty = amount/price) instead of
    the fixed geometric rung — the user's lever to put remaining idle cash to work on demand."""
    monkeypatch.setattr(settings, "sl_pct", 0.0)  # isolate from the SL floor
    row = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=6,
        isolated_fund=100000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])  # fill wave 0
    monkeypatch.setattr(settings, "account_equity", 1_000_000.0)  # plenty of free cash
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"BTC": 50000.0})

    out = service.queue_next_wave(db, row.id, amount_usd=500.0)
    assert abs(out["quantity"] * out["price"] - 500.0) < 1.0  # ~$500 deployed, not the pip rung
    assert abs(out["cost"] - 500.0) < 1.0


def test_dca_next_custom_amount_extends_full_ladder(db, mock_market, monkeypatch):
    """A custom-amount manual DCA+ extends the ladder by one when it's full — a deliberate user
    deploy must not be blocked by max_waves (a plain DCA+ still refuses, see other test)."""
    monkeypatch.setattr(settings, "sl_pct", 0.0)
    row = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=2,
        isolated_fund=100000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])  # fill wave 0 → auto-queues wave 1
    w1 = next(p for p in orders.list_pending(db) if p.source_ref == f"pyramid:{row.id}:wave:1")
    orders.approve_order(db, w1.id)  # fill wave 1 → ladder full (max_waves=2)
    monkeypatch.setattr(settings, "account_equity", 1_000_000.0)
    monkeypatch.setattr("app.market.get_current_prices", lambda syms: {"BTC": 50000.0})

    # Plain DCA+ still refuses on a full ladder; the custom-amount deploy extends it by one.
    with pytest.raises(ValueError, match="Ladder exhausted"):
        service.queue_next_wave(db, row.id)
    out = service.queue_next_wave(db, row.id, amount_usd=300.0)
    assert out["wave_num"] == 2
    db.refresh(row)
    assert row.max_waves == 3  # extended by one for the manual deploy
    assert db.query(models.AuditLog).filter_by(action="dca_extend_ladder").count() == 1


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


def test_wave_below_sl_is_not_queued(db, mock_market, monkeypatch):
    """A DCA rung at/below the SL is a dead order (SL exits first) → it must not be queued.

    distance 10% + SL 8%: after wave 0 fills ~50025 (SL floor≈46023), wave 1 = 45000 < floor.
    (mock_market price 49000 is below the TP at avg×1.03, so on_fill yields next_wave not TP.)
    """
    from app.config import settings
    monkeypatch.setattr(settings, "sl_pct", 8.0)
    row = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=10.0, max_waves=4,
        isolated_fund=100000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])  # fill wave 0 → on_fill wants wave 1 @45000

    waves = db.query(models.KssWave).filter_by(session_id=row.id).all()
    assert [w.wave_num for w in waves] == [0]  # wave 1 (below SL) was skipped
    assert not any(p.source_ref == f"pyramid:{row.id}:wave:1" for p in orders.list_pending(db))
    assert db.query(models.AuditLog).filter_by(action="wave_below_sl").count() == 1
    db.refresh(row)
    assert row.current_wave == 0  # not advanced past the last queued rung

    # Manual DCA must also refuse the dead rung with a clear error.
    with pytest.raises(ValueError, match="dưới SL"):
        service.queue_next_wave(db, row.id)


def test_adjust_distance_blocked_after_fill(db, mock_market):
    """distance_pct is entry-anchored; changing it after a fill would break ladder continuity."""
    row = service.create_session(
        db, symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=4,
        isolated_fund=100000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])  # wave 0 filled

    with pytest.raises(ValueError, match="distance_pct"):
        service.adjust_session(db, row.id, distance_pct=3.0)

    # Other knobs (and re-sending the same distance) are still allowed.
    out = service.adjust_session(db, row.id, tp_pct=5.0, distance_pct=2.0)
    assert out["changes"].get("tp_pct") == 5.0
    db.refresh(row)
    assert row.distance_pct == 2.0  # unchanged
