"""Audit feed renderer: raw events → categorised, human-readable rows."""

from __future__ import annotations

from app import audit, auditview


def _row(db, actor, action, entity=None, **detail):
    r = audit.log(db, actor, action, entity, **detail)
    db.commit()
    return r


def test_categories_and_severity(db):
    R = auditview.render
    assert R(_row(db, "scanner", "session_open", "kss:1", symbol="BTC", mode="auto"))["category"] == "trade"
    stop = R(_row(db, "scheduler", "stop_queued", "kss:2", symbol="ZEC", price=1.0, kind="stop_loss"))
    assert stop["category"] == "risk" and stop["severity"] == "danger" and "Cắt lỗ" in stop["message"]
    defer = R(_row(db, "kss", "tp_deferred", "kss:3", symbol="FET", price=1.0))
    assert defer["category"] == "risk" and defer["severity"] == "warn"
    veto = R(_row(db, "guardian", "veto", "order:7", reason="big"))
    assert veto["category"] == "risk" and "Guardian" in veto["message"]
    close = R(_row(db, "opus", "close", "opos:1", symbol="NIGHT", realized=-8.5))
    assert close["category"] == "opus" and close["severity"] == "danger"
    assert R(_row(db, "scheduler", "cycle", candidates=50))["category"] == "system"


def test_symbol_extraction(db):
    assert auditview.render(_row(db, "opus", "open", "opos:1", symbol="BTC", notional=100))["symbol"] == "BTC"
    # bare-symbol entity (scanner skips)
    assert auditview.render(_row(db, "scanner", "skipped_cooldown", "ETH"))["symbol"] == "ETH"


def test_audit_view_enriched(db):
    _row(db, "scanner", "session_open", "kss:1", symbol="BTC", mode="auto")
    rows = auditview.audit_view(db, limit=10)
    assert rows and {"message", "category", "severity", "icon", "symbol"} <= set(rows[0])
