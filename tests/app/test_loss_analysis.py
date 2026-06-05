"""Loss analysis: cause tagging + breakdowns for the strategy-improvement page."""

from __future__ import annotations

from datetime import datetime

from app import portfolio
from app.models import Fill


def _loss(db, symbol, pnl, ref):
    db.add(Fill(symbol=symbol, side="SELL", quantity=1.0, price=100.0, fee=0.1,
                realized_pnl=pnl, source_ref=ref, executed_at=datetime.utcnow()))


def test_loss_tagging_and_breakdowns(db):
    _loss(db, "NIGHT", -8.56, "opus:3:close")
    _loss(db, "XMR", -3.75, "opus:12:close")
    _loss(db, "FET", -3.88, "pyramid:107:tp")     # 'take-profit' but a loss → flagged
    _loss(db, "ADA", -0.35, "pyramid:130:trailing")
    _loss(db, "BTC", -1.00, "pyramid:9:sl")
    db.add(Fill(symbol="ETH", side="SELL", quantity=1, price=100, realized_pnl=5.0,
                source_ref="pyramid:1:tp", executed_at=datetime.utcnow()))  # a WIN — excluded
    db.commit()

    a = portfolio.loss_analysis(db)
    assert a["count"] == 5
    assert abs(a["total"] - (-17.54)) < 1e-6
    assert a["by_cause"]["OPUS"]["count"] == 2
    assert abs(a["by_cause"]["OPUS"]["total"] - (-12.31)) < 1e-6
    assert a["by_cause"]["KSS-TP?"]["count"] == 1   # the TP-as-loss anomaly is surfaced
    assert "KSS-Trail" in a["by_cause"] and "KSS-SL" in a["by_cause"]
    # worst pair first
    assert a["by_pair"][0][0] == "NIGHT"


def test_tag_mapping():
    assert portfolio._loss_tag("opus:1:close") == "OPUS"
    assert portfolio._loss_tag("pyramid:2:sl") == "KSS-SL"
    assert portfolio._loss_tag("pyramid:2:trailing") == "KSS-Trail"
    assert portfolio._loss_tag("pyramid:2:tp") == "KSS-TP?"
    assert portfolio._loss_tag(None) == "Khác"


def test_empty_is_clean(db):
    a = portfolio.loss_analysis(db)
    assert a["count"] == 0 and a["total"] == 0 and a["rows"] == []
