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
