"""
Telegram notify extensions (plan: telegram-notify-plan.md).

Read commands (/summary /pending /positions /kss /fullauto), categorised event
push with kill switches + throttle, and the periodic digest. `notify.send` is
monkeypatched everywhere so no test touches the network.
"""

from __future__ import annotations

import pytest

from app import notify, orders
from app.config import settings


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Capture every outbound push instead of hitting Telegram; reset throttle state.

    These tests assert proactive-push behaviour, so the master push switch is forced ON
    (it defaults OFF — see test_notify_routing.py for the silent-by-default rule)."""
    monkeypatch.setattr(settings, "telegram_push_enabled", True)
    sent: list[str] = []
    monkeypatch.setattr(notify, "send", lambda text: sent.append(text) or True)
    notify._last_event.clear()
    return sent


@pytest.fixture(autouse=True)
def _stub_prices(monkeypatch):
    monkeypatch.setattr("app.orders.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))
    monkeypatch.setattr("app.portfolio.get_current_prices", lambda syms: dict.fromkeys(syms, 100.0))


# --- Phase 1: read commands ------------------------------------------------

def test_summary_command_reports_equity_and_pnl(db):
    out = notify.handle_command("/summary")
    assert "Equity" in out and "Realized" in out and "Unrealized" in out


def test_pending_command_lists_or_none(db):
    assert "Không có lệnh chờ duyệt" in notify.handle_command("/pending")
    orders.queue_order(db, symbol="BTC", side="BUY", quantity=0.1, price=100.0)
    out = notify.handle_command("/pending")
    assert "BTC" in out and "BUY" in out


def test_positions_command_none_when_empty(db):
    assert "Không có vị thế" in notify.handle_command("/positions")


def test_kss_command_none_when_empty(db):
    assert "Không có phiên KSS" in notify.handle_command("/kss")


def test_fullauto_command_toggles(db, monkeypatch):
    # scheduler.start()/stop() need a running loop; the command's full_auto effect is
    # what matters here, so stub the loop lifecycle.
    monkeypatch.setattr("app.scheduler.start", lambda: True)
    monkeypatch.setattr("app.scheduler.stop", lambda: True)
    assert "ON" in notify.handle_command("/fullauto on")
    assert settings.full_auto is True
    assert "OFF" in notify.handle_command("/fullauto off")
    assert settings.full_auto is False
    assert "Dùng:" in notify.handle_command("/fullauto")  # missing arg → usage


def test_help_lists_new_commands(db):
    h = notify.handle_command("/help")
    for cmd in ("/summary", "/pending", "/positions", "/kss", "/fullauto"):
        assert cmd in h


# --- Phase 2: categorised push + kill switches + throttle ------------------

def test_event_trade_kill_switch(monkeypatch, _no_network):
    monkeypatch.setattr(settings, "telegram_notify_trades", False)
    assert notify.event("trade", "x") is False and _no_network == []
    monkeypatch.setattr(settings, "telegram_notify_trades", True)
    assert notify.event("trade", "y") is True and _no_network == ["y"]


def test_event_risk_kill_switch(monkeypatch, _no_network):
    monkeypatch.setattr(settings, "telegram_notify_risk", False)
    assert notify.event("risk", "x") is False and _no_network == []
    monkeypatch.setattr(settings, "telegram_notify_risk", True)
    assert notify.event("risk", "y") is True and _no_network == ["y"]


def test_trade_throttle_coalesces_same_symbol(monkeypatch, _no_network):
    monkeypatch.setattr(settings, "telegram_notify_trades", True)
    notify.event("trade", "a", throttle_key="BTC", cooldown=8.0)
    notify.event("trade", "b", throttle_key="BTC", cooldown=8.0)   # within cooldown → dropped
    notify.event("trade", "c", throttle_key="ETH", cooldown=8.0)   # different symbol → sent
    assert _no_network == ["a", "c"]


def test_fill_alert_routes_sl_to_risk_and_buy_to_trade(monkeypatch, _no_network):
    monkeypatch.setattr(settings, "telegram_notify_trades", True)
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    class _F:  # minimal fill stand-in
        def __init__(self, side, ref, pnl=0.0):
            self.symbol, self.quantity, self.price = "BTC", 1.0, 100.0
            self.side, self.source_ref, self.realized_pnl = side, ref, pnl

    notify.fill_alert(_F("BUY", "kss:1"))
    notify.fill_alert(_F("SELL", "kss:1:sl", -5.0))
    assert any("🟢 BUY" in m for m in _no_network)
    assert any("🛑 SL" in m and "PnL" in m for m in _no_network)


def test_risk_fill_not_throttled(monkeypatch, _no_network):
    monkeypatch.setattr(settings, "telegram_notify_risk", True)

    class _F:
        symbol, quantity, price, side = "BTC", 1.0, 100.0, "SELL"
        source_ref, realized_pnl = "kss:1:sl", -2.0

    notify.fill_alert(_F())  # first risk push
    notify.fill_alert(_F())  # second risk push — risk is never throttled
    assert len(_no_network) == 2


def test_approve_order_pushes_fill(db, monkeypatch, _no_network):
    monkeypatch.setattr(settings, "telegram_notify_trades", True)
    b, _ = orders.queue_order(db, symbol="ETH", side="BUY", quantity=0.5, price=100.0)
    orders.approve_order(db, b.id)
    assert any("ETH" in m for m in _no_network)


# --- Phase 3: digest -------------------------------------------------------

def test_build_digest_has_equity_and_today(db):
    d = notify.build_digest(db)
    assert "Equity" in d and "Hôm nay" in d


def test_maybe_send_digest_off_when_zero(db, monkeypatch, _no_network):
    monkeypatch.setattr(settings, "telegram_digest_hours", 0)
    assert notify.maybe_send_digest(db) is False and _no_network == []


def test_maybe_send_digest_sends_when_enabled(db, monkeypatch, _no_network):
    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(settings, "telegram_digest_hours", 6)
    assert notify.maybe_send_digest(db) is True
    assert any("digest" in m for m in _no_network)
    # second immediate call is throttled by the 6h window
    assert notify.maybe_send_digest(db) is False
