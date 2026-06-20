"""
Tests for app.notify — Telegram alert sender + command handler.

No network calls are ever made: httpx is not invoked in any test.
"""

from __future__ import annotations

from pydantic import SecretStr

from app import notify, runtime
from app.config import settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enable_telegram(monkeypatch) -> None:
    """Flip settings so notify.enabled() returns True."""
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("bot123:TOKEN"))
    monkeypatch.setattr(settings, "telegram_chat_id", "99999")


def _noop_send(monkeypatch) -> None:
    """Replace notify.send so commands that call it don't hit the network."""
    monkeypatch.setattr("app.notify.send", lambda text: False)


# ---------------------------------------------------------------------------
# enabled()
# ---------------------------------------------------------------------------


def test_enabled_false_by_default():
    assert notify.enabled() is False


def test_enabled_true_when_all_configured(monkeypatch):
    _enable_telegram(monkeypatch)
    assert notify.enabled() is True


def test_enabled_false_without_token(monkeypatch):
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr(""))
    monkeypatch.setattr(settings, "telegram_chat_id", "99999")
    assert notify.enabled() is False


def test_enabled_false_without_chat_id(monkeypatch):
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("bot123:TOKEN"))
    monkeypatch.setattr(settings, "telegram_chat_id", "")
    assert notify.enabled() is False


def test_enabled_false_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "telegram_enabled", False)
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("bot123:TOKEN"))
    monkeypatch.setattr(settings, "telegram_chat_id", "99999")
    assert notify.enabled() is False


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------


def test_send_returns_false_when_disabled():
    # telegram_enabled=False by default — no network call attempted
    result = notify.send("hello")
    assert result is False


# ---------------------------------------------------------------------------
# handle_command("/help")
# ---------------------------------------------------------------------------


def test_handle_command_help_lists_commands():
    reply = notify.handle_command("/help")
    for cmd in ("/status", "/pause", "/resume", "/freeze", "/reset", "/help"):
        assert cmd in reply


def test_handle_command_empty_string_returns_help():
    reply = notify.handle_command("")
    assert "/help" in reply


def test_handle_command_unknown_returns_help_hint():
    reply = notify.handle_command("/foobar")
    assert "/foobar" in reply
    assert "/help" in reply


# ---------------------------------------------------------------------------
# handle_command("/trade")
# ---------------------------------------------------------------------------


def test_help_lists_trade_command():
    assert "/trade" in notify.handle_command("/help")


def test_handle_command_trade_empty(db):
    assert "Chưa có giao dịch" in notify.handle_command("/trade")


def test_handle_command_trade_lists_recent_with_sell_pnl(db):
    """Lists recent fills; a SELL shows its realized pnl, a BUY does not."""
    from app.db import SessionLocal
    from app.models import Fill

    s = SessionLocal()
    try:
        s.add(Fill(symbol="BTC", side="BUY", quantity=2, price=100.0, fee=0.1,
                   source_ref="pyramid:1:wave:0"))
        s.add(Fill(symbol="BTC", side="SELL", quantity=2, price=110.0, fee=0.1,
                   realized_pnl=19.8, source_ref="pyramid:1:tp"))
        s.commit()
    finally:
        s.close()

    reply = notify.handle_command("/trade")
    assert "Trades" in reply and "BTC" in reply
    assert "SELL" in reply and "pnl" in reply        # SELL line carries realized pnl
    # the alias resolves to the same handler
    assert notify.handle_command("/trades").startswith("🧾")


def _seed_trades(n_buy: int, n_sell: int) -> None:
    from app.db import SessionLocal
    from app.models import Fill

    s = SessionLocal()
    try:
        for i in range(n_buy):
            s.add(Fill(symbol="ETH", side="BUY", quantity=1, price=100.0 + i,
                       source_ref="pyramid:1:wave:0"))
        for i in range(n_sell):
            s.add(Fill(symbol="ETH", side="SELL", quantity=1, price=110.0 + i,
                       realized_pnl=9.9, source_ref="pyramid:1:tp"))
        s.commit()
    finally:
        s.close()


def test_handle_command_trade_side_filter(db):
    _seed_trades(n_buy=3, n_sell=2)
    sell = notify.handle_command("/trade sell")
    assert "Trades SELL" in sell and "BUY" not in sell
    buy = notify.handle_command("/trade buy")
    assert "Trades BUY" in buy and "SELL" not in buy


def test_handle_command_trade_count_arg(db):
    _seed_trades(n_buy=8, n_sell=0)
    reply = notify.handle_command("/trade 3")
    # header + exactly 3 trade lines
    assert len(reply.splitlines()) == 1 + 3


def test_handle_command_trade_empty_side(db):
    _seed_trades(n_buy=2, n_sell=0)   # no SELLs
    assert "Chưa có lệnh SELL" in notify.handle_command("/trade sell")


# ---------------------------------------------------------------------------
# handle_command("/positions") — must not silently drop positions past #15
# ---------------------------------------------------------------------------


def test_positions_command_shows_big_position_beyond_15(db, monkeypatch):
    """Regression: with >15 positions, the old rows[:15] (insertion order) dropped sizeable coins
    like STG. Now sorted biggest-first and capped high → the big late one always shows."""
    from app import portfolio
    from app.db import SessionLocal
    from app.models import Position

    s = SessionLocal()
    try:
        for i in range(15):  # 15 tiny positions inserted first
            s.add(Position(symbol=f"C{i}", quantity=1.0, avg_entry_price=1.0, total_cost=1.0))
        s.add(Position(symbol="STG", quantity=7000.0, avg_entry_price=0.2, total_cost=1400.0))  # #16
        s.commit()
    finally:
        s.close()
    monkeypatch.setattr(portfolio, "get_current_prices",
                        lambda syms: {sym: (0.21 if sym == "STG" else 1.0) for sym in syms})
    reply = notify.handle_command("/positions")
    assert "STG" in reply           # the big 16th position is shown (was cut by rows[:15] before)
    assert "Positions (16" in reply  # header reflects the true count


def test_pending_command_counts_beyond_15(db):
    from app.db import SessionLocal
    from app.models import PENDING, PendingOrder

    s = SessionLocal()
    try:
        for i in range(16):
            s.add(PendingOrder(symbol=f"C{i}", side="BUY", quantity=1, price=1.0,
                               order_type="LIMIT", status=PENDING, source="kss"))
        s.commit()
    finally:
        s.close()
    reply = notify.handle_command("/pending")
    assert "Pending (16" in reply  # all 16 counted, not silently cut at 15


def test_kss_command_lists_beyond_15(db, monkeypatch):
    from app import market
    from app.db import SessionLocal
    from app.models import SESSION_ACTIVE, KssSession

    s = SessionLocal()
    try:
        for i in range(16):
            s.add(KssSession(symbol=f"K{i}", entry_price=1.0, distance_pct=2.0, max_waves=6,
                             isolated_fund=100.0, tp_pct=4.0, timeout_x_min=1.0, gap_y_min=0.0,
                             status=SESSION_ACTIVE, total_filled_qty=1.0, avg_price=1.0, total_cost=1.0))
        s.commit()
    finally:
        s.close()
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {x: 1.0 for x in syms})
    reply = notify.handle_command("/kss")
    assert reply.count("[active]") == 16  # all 16 sessions listed (old limit=15 dropped one)


# ---------------------------------------------------------------------------
# handle_command("/status")
# ---------------------------------------------------------------------------


def test_handle_command_status_returns_state_keys(db, monkeypatch):
    """status reply must mention known state labels; no network needed."""
    _noop_send(monkeypatch)
    reply = notify.handle_command("/status")
    assert "frozen" in reply
    assert "scheduler" in reply


# ---------------------------------------------------------------------------
# handle_command("/freeze") and "/reset"
# ---------------------------------------------------------------------------


def test_freeze_command_sets_frozen_state(db, monkeypatch):
    _noop_send(monkeypatch)
    reply = notify.handle_command("/freeze")
    assert "frozen" in reply.lower()

    # Verify DB state using a fresh session (same underlying DB file).
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        assert runtime.is_frozen(s) is True
    finally:
        s.close()


def test_reset_command_clears_frozen_state(db, monkeypatch):
    _noop_send(monkeypatch)
    # First freeze via handle_command, then reset
    notify.handle_command("/freeze")
    reply = notify.handle_command("/reset")
    assert "reset" in reply.lower() or "unblocked" in reply.lower()

    from app.db import SessionLocal
    s = SessionLocal()
    try:
        assert runtime.is_frozen(s) is False
    finally:
        s.close()


def test_freeze_then_reset_roundtrip(db, monkeypatch):
    _noop_send(monkeypatch)
    notify.handle_command("/freeze")
    notify.handle_command("/reset")

    from app.db import SessionLocal
    s = SessionLocal()
    try:
        assert runtime.is_frozen(s) is False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# handle_command("/pause") — scheduler.stop() is safe with no running loop
# ---------------------------------------------------------------------------


def test_pause_command_returns_paused_reply(db, monkeypatch):
    _noop_send(monkeypatch)
    # scheduler.stop() is safe even with no running task
    reply = notify.handle_command("/pause")
    assert "paused" in reply.lower() or "stopped" in reply.lower() or "disabled" in reply.lower()


def test_pause_turns_off_full_auto(db, monkeypatch):
    _noop_send(monkeypatch)
    # Turn full_auto on first so we can verify pause turns it off
    monkeypatch.setattr(settings, "full_auto", True)
    monkeypatch.setattr(settings, "auto_trade", True)
    monkeypatch.setattr(settings, "autoapprove_enabled", True)

    notify.handle_command("/pause")
    assert settings.full_auto is False


# ---------------------------------------------------------------------------
# handle_command("/resume") — monkeypatch scheduler.start to avoid event loop
# ---------------------------------------------------------------------------


def test_resume_command_returns_resumed_reply(db, monkeypatch):
    _noop_send(monkeypatch)
    # Avoid asyncio.create_task outside an event loop
    monkeypatch.setattr("app.scheduler.start", lambda: False)

    reply = notify.handle_command("/resume")
    assert "resumed" in reply.lower() or "enabled" in reply.lower() or "started" in reply.lower()
