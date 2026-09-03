"""P1 Fix 4 — a dead ladder (on_fill couldn't afford the next wave) must leave an audit trail.

Before this fix, ``PyramidSession.on_fill``'s insufficient-fund branch returned only a free-text
``message`` and logged a ``logger.warning`` — invisible to the DB audit trail a starved session
otherwise leaves no record of why it stopped growing. The fix adds a machine-readable
``reason``/``need``/``remaining`` to the returned dict (tested in test_kss.py) and this service-
layer audit, deduped to at most once per (session, wave) so a re-evaluated shortfall does not
flood the trail with duplicates of the same dead rung.
"""

from __future__ import annotations

import pytest

from app import orders
from app.config import settings
from app.kss import service
from app.models import AuditLog, KssSession


@pytest.fixture
def mock_market(monkeypatch):
    ex_info = {"minQty": 0.00001, "stepSize": 0.00001, "maxQty": 10000.0}
    monkeypatch.setattr("app.kss.pyramid.get_exchange_info", lambda s: ex_info)
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {})
    monkeypatch.setattr("app.market.get_current_prices", lambda syms, force=False: {})


def _session(db, **kw):
    d = {"symbol": "AAA", "entry_price": 100.0, "distance_pct": 1.5, "max_waves": 6,
         "isolated_fund": 100000.0, "tp_pct": 4.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0,
         "sl_pct": 90.0}
    d.update(kw)
    s = KssSession(**d)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_insufficient_fund_audited_once_via_handle_fill_event(db, mock_market, monkeypatch):
    """A starved ladder (isolated_fund exhausted between wave 1 and wave 2) writes exactly one
    ``insufficient_fund`` audit row, carrying the session/symbol/wave/need/remaining."""
    monkeypatch.setattr(settings, "kss_first_wave_usd", 15.0)
    row = service.create_session(
        db, symbol="XYZ", entry_price=100.0, distance_pct=2.0, max_waves=5,
        isolated_fund=54.4, tp_pct=90.0, timeout_x_min=1440.0, gap_y_min=0.0,
    )
    res = service.start_session(db, row.id)
    orders.approve_order(db, res["pending_order_id"])  # wave 0 filled ($15) -> queues wave 1 ($29.4)
    w1 = next(p for p in orders.list_pending(db) if p.source_ref == f"pyramid:{row.id}:wave:1")
    orders.approve_order(db, w1.id)  # wave 1 filled -> wave 2 ($43.22) needed, only $10 left

    rows = db.query(AuditLog).filter(AuditLog.action == "insufficient_fund").all()
    assert len(rows) == 1
    import json
    detail = json.loads(rows[0].detail)
    assert detail["symbol"] == "XYZ"
    assert detail["wave"] == 2
    assert detail["need"] > detail["remaining"] >= 0

    # No wave-2 order was queued — the ladder is genuinely starved, not just under-reported.
    assert not any(p.source_ref == f"pyramid:{row.id}:wave:2" for p in orders.list_pending(db))


def test_insufficient_fund_audit_dedupes_per_wave(db):
    """Three re-evaluations of the SAME shortfall (e.g. three manage cycles hitting the same
    unaffordable wave) write exactly ONE audit row; once the ladder advances to a NEW wave, a
    fresh shortfall audits again."""
    row = _session(db)
    result_wave2 = {"reason": "insufficient_fund", "wave_num": 2, "need": 100.0, "remaining": 10.0}

    service._audit_insufficient_fund(db, row, result_wave2)
    service._audit_insufficient_fund(db, row, result_wave2)
    service._audit_insufficient_fund(db, row, result_wave2)
    db.commit()

    rows = db.query(AuditLog).filter(AuditLog.action == "insufficient_fund").all()
    assert len(rows) == 1

    result_wave3 = {"reason": "insufficient_fund", "wave_num": 3, "need": 200.0, "remaining": 5.0}
    service._audit_insufficient_fund(db, row, result_wave3)
    db.commit()

    rows = db.query(AuditLog).filter(AuditLog.action == "insufficient_fund").all()
    assert len(rows) == 2
