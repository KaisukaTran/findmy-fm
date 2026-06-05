"""Drawdown fixes: exit SELLs never blocked by entry-risk / veto; per-symbol cap; shadow toggle."""

from __future__ import annotations

import pytest

from app import orders, risk, runtime, scanner
from app.config import settings
from app.models import PENDING, PendingOrder


@pytest.fixture(autouse=True)
def _mock_market(monkeypatch):
    monkeypatch.setattr(risk, "account_equity", lambda db: 10000.0)
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))


# --- risk: exits bypass entry gates ------------------------------------


def test_sell_never_flagged_by_position_size(db):
    # A BUY this large would breach the 10% cap; a SELL must not.
    buy_ok, buy_v = risk.check_all_risks("BTC", qty=20.0, price=100.0, db=db, side="BUY")
    sell_ok, sell_v = risk.check_all_risks("BTC", qty=20.0, price=100.0, db=db, side="SELL")
    assert buy_ok is False and buy_v  # 20*100=2000 = 20% > 10%
    assert sell_ok is True and sell_v == []


def test_queue_sell_carries_no_risk_note(db):
    order, note = orders.queue_order(db, symbol="BTC", side="SELL", quantity=20.0, price=100.0)
    assert note is None and order.risk_note is None


# --- auto-fill: vetoed exit still fills --------------------------------


def test_vetoed_sell_still_auto_fills(db, monkeypatch):
    o = PendingOrder(symbol="BTC", side="SELL", order_type="MARKET", quantity=1.0, price=0.0,
                     source="kss", source_ref="pyramid:1:tp", status=PENDING,
                     auto_veto=True, auto_veto_reason="stale")
    db.add(o)
    db.commit()
    # give the symbol a position so the SELL can execute
    from app.models import Position
    db.add(Position(symbol="BTC", quantity=1.0, avg_entry_price=90.0, total_cost=90.0))
    db.commit()
    approved = orders.auto_fill_due_orders(db)
    assert o.id in approved  # vetoed exit was filled


def test_vetoed_buy_stays_blocked(db):
    o = PendingOrder(symbol="BTC", side="BUY", order_type="MARKET", quantity=0.1, price=0.0,
                     source="kss", source_ref="pyramid:2:wave:1", status=PENDING,
                     auto_veto=True, auto_veto_reason="guardian")
    db.add(o)
    db.commit()
    assert orders.auto_fill_due_orders(db) == []  # vetoed BUY held back


# --- per-symbol concentration cap --------------------------------------


def test_symbol_session_cap(db, monkeypatch):
    from app.models import SESSION_ACTIVE, KssSession
    monkeypatch.setattr(settings, "max_sessions_per_symbol", 2)
    assert scanner._symbol_at_cap(db, "HYPE") is False
    for _ in range(2):
        db.add(KssSession(symbol="HYPE", entry_price=1, distance_pct=2, max_waves=5,
                          isolated_fund=100, tp_pct=3, timeout_x_min=1, gap_y_min=0,
                          status=SESSION_ACTIVE))
    db.commit()
    assert scanner._symbol_at_cap(db, "HYPE") is True
    monkeypatch.setattr(settings, "max_sessions_per_symbol", 0)  # disabled
    assert scanner._symbol_at_cap(db, "HYPE") is False


# --- SELL never oversells (no phantom realized profit) -----------------


def test_sell_on_empty_position_books_no_profit(db):
    from app.models import Position
    db.add(Position(symbol="BTC", quantity=0.0, avg_entry_price=0.0, total_cost=0.0))
    db.commit()
    realized = orders._update_position(db, "BTC", "SELL", qty=5.0, price=100.0, fee=0.0)
    assert realized == 0.0  # nothing held → no phantom proceeds


def test_sell_clamped_to_held_qty(db):
    from app.models import Position
    db.add(Position(symbol="BTC", quantity=1.0, avg_entry_price=90.0, total_cost=90.0))
    db.commit()
    # try to sell 3 but only 1 held → realize on 1 only: (100-90)*1 = 10
    realized = orders._update_position(db, "BTC", "SELL", qty=3.0, price=100.0, fee=0.0)
    assert abs(realized - 10.0) < 1e-9
    pos = db.query(Position).filter(Position.symbol == "BTC").one()
    assert pos.quantity == 0.0


# --- shadow toggle persistence -----------------------------------------


def test_opus_shadow_toggle_persists(db):
    runtime.opus_shadow_set(db, False)
    assert settings.opus_shadow is False
    settings.opus_shadow = True  # simulate process restart default
    runtime.sync_from_db(db)
    assert settings.opus_shadow is False  # restored from runtime_config
