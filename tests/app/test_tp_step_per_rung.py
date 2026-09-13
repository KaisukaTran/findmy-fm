"""`kss_tp_step_per_rung_pct`: the take-profit climbs with every filled DCA rung.

Product rule (2026-09-12): base TP 5%, step 0.5%/rung — once rung 8 has filled (waves 0..8)
the exit sits 9% above the average, not 5%. The effective TP is DERIVED in `_to_pyramid` from
the untouched `row.tp_pct`; it is never written to the row, so a replayed fill or a restart can
never bump it twice. `app/kss/pyramid.py` is frozen and reads `self.tp_pct`, so one assignment
at load time serves both the paper `check_tp` and the live resting exit.
"""

from app import costengine, execution, models, orders
from app.config import settings
from app.kss import service
from app.models import KssSession, KssWave


def _session(db, *, tp_pct=5.0, filled_rungs=0, pending_rungs=0) -> KssSession:
    row = KssSession(
        symbol="SOL", entry_price=10.0, distance_pct=2.0, max_waves=10, isolated_fund=5000.0,
        tp_pct=tp_pct, timeout_x_min=60, gap_y_min=5, status=models.SESSION_ACTIVE,
        current_wave=filled_rungs, avg_price=9.0, total_filled_qty=10.0, total_cost=90.0,
    )
    db.add(row)
    db.flush()
    db.add(KssWave(session_id=row.id, wave_num=0, quantity=1.0, target_price=10.0,
                   status=models.WAVE_FILLED, filled_qty=1.0, filled_price=10.0))
    for n in range(1, filled_rungs + 1):
        db.add(KssWave(session_id=row.id, wave_num=n, quantity=n + 1.0, target_price=10 - n * 0.2,
                       status=models.WAVE_FILLED, filled_qty=n + 1.0, filled_price=10 - n * 0.2))
    for n in range(filled_rungs + 1, filled_rungs + 1 + pending_rungs):
        db.add(KssWave(session_id=row.id, wave_num=n, quantity=n + 1.0, target_price=10 - n * 0.2,
                       status=models.WAVE_PENDING))
    db.commit()
    db.refresh(row)
    return row


def test_step_off_is_byte_identical(db):
    settings.kss_tp_step_per_rung_pct = 0.0
    row = _session(db, filled_rungs=8)
    py = service._to_pyramid(row)
    assert py.tp_pct == 5.0
    assert py.estimated_tp_price == 9.0 * (1 + (5.0 + costengine.tp_fee_buffer_pct()) / 100)


def test_rung_8_filled_lifts_a_5pct_tp_to_9pct(db):
    settings.kss_tp_step_per_rung_pct = 0.5
    row = _session(db, filled_rungs=8)
    py = service._to_pyramid(row)
    assert py.tp_pct == 9.0
    assert py.estimated_tp_price == 9.0 * (1 + (9.0 + costengine.tp_fee_buffer_pct()) / 100)


def test_only_the_entry_filled_keeps_the_base_tp(db):
    settings.kss_tp_step_per_rung_pct = 0.5
    py = service._to_pyramid(_session(db, filled_rungs=0, pending_rungs=3))
    assert py.tp_pct == 5.0


def test_pending_rungs_do_not_count(db):
    settings.kss_tp_step_per_rung_pct = 0.5
    py = service._to_pyramid(_session(db, filled_rungs=2, pending_rungs=5))
    assert py.tp_pct == 6.0


def test_the_row_keeps_its_base_tp_across_a_state_round_trip(db):
    settings.kss_tp_step_per_rung_pct = 0.5
    row = _session(db, filled_rungs=4)
    py = service._to_pyramid(row)
    service._save_state(row, py)
    db.commit()
    db.refresh(row)
    assert row.tp_pct == 5.0
    assert service._to_pyramid(row).tp_pct == 7.0, "derived again from the base, not stacked"


def test_paper_check_tp_uses_the_lifted_target(db):
    """The frozen engine's own exit check sees the derived value."""
    settings.kss_tp_step_per_rung_pct = 0.5
    py = service._to_pyramid(_session(db, filled_rungs=8))
    base_target = 9.0 * (1 + (5.0 + costengine.tp_fee_buffer_pct()) / 100)
    assert py.check_tp(base_target * 1.001) is None, "5% is no longer enough"
    assert py.check_tp(py.estimated_tp_price * 1.001) is not None


def test_live_resting_tp_is_replaced_higher_after_a_rung_fills(db, monkeypatch):
    """Live: the resting exit follows the derived target through sync_resting_tp's drift path."""
    class _Provider:
        def pair(self, symbol):
            return f"{symbol}/USDT"

    monkeypatch.setattr(execution, "live_enabled", lambda: True)
    monkeypatch.setattr("app.data.providers.live_provider", lambda: _Provider())
    monkeypatch.setattr(execution, "cancel_live_order", lambda pair, oid: None)
    monkeypatch.setattr(execution, "fetch_live_order", lambda pair, oid: {
        "status": "canceled", "filled": 0.0, "average": 0.0, "fee": 0.0, "raw_id": oid})
    monkeypatch.setattr(execution, "place_live_order",
                        lambda *a, **k: {"raw_id": "T1", "status": "open", "price": 0.0,
                                         "quantity": 0.0, "fee": 0.0})
    settings.maker_orders = True
    settings.auto_trade = True
    settings.kss_tp_step_per_rung_pct = 0.5
    row = _session(db, filled_rungs=1)

    service.sync_resting_tp(db)
    tp = (db.query(models.PendingOrder)
          .filter(models.PendingOrder.source_ref == f"pyramid:{row.id}:tp").one())
    price_after_one_rung = tp.price

    wave = db.query(KssWave).filter_by(session_id=row.id, wave_num=1).one()
    db.add(KssWave(session_id=row.id, wave_num=2, quantity=3.0, target_price=wave.target_price,
                   status=models.WAVE_FILLED, filled_qty=3.0, filled_price=wave.target_price))
    db.commit()
    orders.sync_resting_orders(db)  # link it, so the replace goes through the cancel path
    service.sync_resting_tp(db)
    db.refresh(tp)

    assert tp.price > price_after_one_rung
    assert abs(tp.price / row.avg_price - (1 + (6.0 + costengine.tp_fee_buffer_pct()) / 100)) < 1e-9


def test_preview_lifts_the_tp_per_wave(db):
    settings.kss_tp_step_per_rung_pct = 0.5
    out = service.preview("SOL", 10.0, 2.0, 10, 1000.0, 5.0)
    w8 = out["waves"][8]
    assert w8["tp_price_after"] == round(w8["avg_price_after"] * (1 + 9.0 / 100), 8)
    assert out["waves"][0]["tp_price_after"] == round(out["waves"][0]["avg_price_after"] * 1.05, 8)


def test_knob_round_trips_through_the_settings_api(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/kss-settings", json={"kss_tp_step_per_rung_pct": 0.5})
        assert r.status_code == 200
        assert c.get("/api/kss-settings").json()["kss_tp_step_per_rung_pct"] == 0.5
        assert settings.kss_tp_step_per_rung_pct == 0.5
        assert c.post("/api/kss-settings", json={"kss_tp_step_per_rung_pct": 11}).status_code == 422
