"""Schema + basic ORM round-trip tests for the lean models."""

from sqlalchemy import inspect

from app import models
from app.db import engine


def test_all_tables_created():
    tables = set(inspect(engine).get_table_names())
    assert {"pending_orders", "fills", "positions", "kss_sessions", "kss_waves"} <= tables


def test_pending_order_roundtrip(db):
    order = models.PendingOrder(symbol="BTC", side="BUY", quantity=0.001, price=65000.0)
    db.add(order)
    db.commit()
    db.refresh(order)
    assert order.id is not None
    assert order.status == models.PENDING
    d = order.to_dict()
    assert d["symbol"] == "BTC" and d["status"] == "pending"


def test_session_wave_cascade(db):
    s = models.KssSession(
        symbol="BTC", entry_price=50000.0, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
    )
    s.waves.append(models.KssWave(wave_num=0, quantity=0.00002, target_price=50000.0))
    db.add(s)
    db.commit()
    db.refresh(s)
    assert s.id is not None
    assert len(s.waves) == 1
    assert s.status == models.SESSION_PENDING

    db.delete(s)
    db.commit()
    assert db.query(models.KssWave).count() == 0  # cascade delete
