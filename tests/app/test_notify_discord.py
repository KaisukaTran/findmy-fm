"""
Tests for app.notify_discord — Discord webhook push + the notify.send fan-out.

No real network calls are made: httpx.post is monkeypatched in every test that
exercises the sender. The command gateway (websockets) is not started here; only
its pure message-dispatch + auth boundary is unit-tested.
"""

from __future__ import annotations

from pydantic import SecretStr

from app import notify, notify_discord
from app.config import settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enable_webhook(monkeypatch) -> None:
    monkeypatch.setattr(settings, "discord_enabled", True)
    monkeypatch.setattr(
        settings, "discord_webhook_url", SecretStr("https://discord.com/api/webhooks/1/abc")
    )


def _enable_commands(monkeypatch) -> None:
    monkeypatch.setattr(settings, "discord_enabled", True)
    monkeypatch.setattr(settings, "discord_bot_token", SecretStr("bot.token.value"))
    monkeypatch.setattr(settings, "discord_channel_id", "123456789")


class _FakeResp:
    def __init__(self, code: int):
        self.status_code = code


# ---------------------------------------------------------------------------
# enablement gates
# ---------------------------------------------------------------------------


def test_webhook_and_commands_off_by_default():
    assert notify_discord.webhook_enabled() is False
    assert notify_discord.command_enabled() is False


def test_webhook_enabled_when_url_set(monkeypatch):
    _enable_webhook(monkeypatch)
    assert notify_discord.webhook_enabled() is True


def test_commands_enabled_when_token_and_channel_set(monkeypatch):
    _enable_commands(monkeypatch)
    assert notify_discord.command_enabled() is True


def test_webhook_disabled_when_master_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "discord_enabled", False)
    monkeypatch.setattr(
        settings, "discord_webhook_url", SecretStr("https://discord.com/api/webhooks/1/abc")
    )
    assert notify_discord.webhook_enabled() is False


# ---------------------------------------------------------------------------
# send() — webhook push
# ---------------------------------------------------------------------------


def test_send_returns_false_when_disabled():
    # disabled by default — no network call attempted
    assert notify_discord.send("hello") is False


def test_send_posts_to_webhook_and_returns_true_on_204(monkeypatch):
    _enable_webhook(monkeypatch)
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp(204)

    monkeypatch.setattr("app.notify_discord.httpx.post", fake_post)
    assert notify_discord.send("trade filled") is True
    assert captured["url"] == "https://discord.com/api/webhooks/1/abc"
    assert captured["json"]["content"] == "trade filled"


def test_send_truncates_to_2000_chars(monkeypatch):
    _enable_webhook(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        "app.notify_discord.httpx.post",
        lambda url, json=None, timeout=None: captured.update(json=json) or _FakeResp(200),
    )
    notify_discord.send("x" * 5000)
    assert len(captured["json"]["content"]) == 2000


def test_send_swallows_network_error(monkeypatch):
    _enable_webhook(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.notify_discord.httpx.post", boom)
    assert notify_discord.send("x") is False  # never raises


# ---------------------------------------------------------------------------
# notify.send fan-out: one alert reaches every enabled channel
# ---------------------------------------------------------------------------


def test_notify_send_fans_out_to_discord(monkeypatch):
    # Telegram off (default) but Discord webhook on → notify.send must still deliver.
    _enable_webhook(monkeypatch)
    seen = {}

    def fake_discord_send(text):
        seen["text"] = text
        return True

    monkeypatch.setattr("app.notify._telegram_send", lambda text: False)
    monkeypatch.setattr("app.notify_discord.send", fake_discord_send)

    assert notify.send("breaker frozen") is True
    assert seen["text"] == "breaker frozen"


def test_any_channel_enabled_true_for_discord_only(monkeypatch):
    _enable_webhook(monkeypatch)
    # Telegram stays disabled by default
    assert notify.enabled() is False
    assert notify.any_channel_enabled() is True


# ---------------------------------------------------------------------------
# command gateway — auth boundary on inbound messages
# ---------------------------------------------------------------------------


def test_dispatch_ignores_other_channel(monkeypatch):
    _enable_commands(monkeypatch)
    replies = []
    monkeypatch.setattr("app.notify_discord._reply", lambda ch, txt: replies.append((ch, txt)))
    # message from a DIFFERENT channel id must be dropped
    notify_discord._dispatch_message(
        {"channel_id": "999", "content": "/help", "author": {"bot": False}}
    )
    assert replies == []


def test_dispatch_ignores_bot_authors(monkeypatch):
    _enable_commands(monkeypatch)
    replies = []
    monkeypatch.setattr("app.notify_discord._reply", lambda ch, txt: replies.append((ch, txt)))
    notify_discord._dispatch_message(
        {"channel_id": "123456789", "content": "/help", "author": {"bot": True}}
    )
    assert replies == []


def test_dispatch_runs_command_for_authorized_channel(monkeypatch):
    _enable_commands(monkeypatch)
    replies = []
    monkeypatch.setattr("app.notify_discord._reply", lambda ch, txt: replies.append((ch, txt)))
    notify_discord._dispatch_message(
        {"channel_id": "123456789", "content": "/help", "author": {"bot": False}}
    )
    assert len(replies) == 1
    channel, text = replies[0]
    assert channel == "123456789"
    assert "/status" in text  # the shared help text from notify.handle_command


def test_dispatch_ignores_non_slash_text(monkeypatch):
    _enable_commands(monkeypatch)
    replies = []
    monkeypatch.setattr("app.notify_discord._reply", lambda ch, txt: replies.append((ch, txt)))
    notify_discord._dispatch_message(
        {"channel_id": "123456789", "content": "hello there", "author": {"bot": False}}
    )
    assert replies == []
