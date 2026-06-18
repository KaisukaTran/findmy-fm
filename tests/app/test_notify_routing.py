"""
Tests for the paper+live parallel Telegram model:
  * instance labelling (🧪 PAPER / 🔴 LIVE) applied inside send()
  * send(instance=...) override for relayed (routed) replies
  * start() gating via telegram_poll_commands (alerts-only secondary instance)
  * _split_target() command/target grammar
  * _proxy_command() sibling routing (no network — httpx.post is stubbed)
  * the /internal/telegram/command endpoint auth boundary

No real network call is made in any test.
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import SecretStr

from app import notify
from app.config import settings


# ---------------------------------------------------------------------------
# instance_name() / _label()
# ---------------------------------------------------------------------------


def test_instance_name_paper_by_default(monkeypatch):
    monkeypatch.setattr(settings, "live_trading", False)
    assert notify.instance_name() == "paper"
    assert notify._label("paper") == "🧪 PAPER"


def test_instance_name_live_when_live_trading(monkeypatch):
    monkeypatch.setattr(settings, "live_trading", True)
    assert notify.instance_name() == "live"
    assert notify._label("live") == "🔴 LIVE"


# ---------------------------------------------------------------------------
# send() labelling
# ---------------------------------------------------------------------------


def _capture_telegram(monkeypatch) -> list[str]:
    """Stub the actual Telegram transmit + disable Discord; capture the final text."""
    sent: list[str] = []
    monkeypatch.setattr(notify, "_telegram_send", lambda text: sent.append(text) or True)
    monkeypatch.setattr("app.notify_discord.webhook_enabled", lambda: False)
    return sent


def test_send_prefixes_local_label(monkeypatch):
    monkeypatch.setattr(settings, "live_trading", False)
    sent = _capture_telegram(monkeypatch)
    notify.send("hello")
    assert sent == ["🧪 PAPER hello"]


def test_send_instance_override_labels_target(monkeypatch):
    """A reply relayed on behalf of the sibling is tagged with the TARGET's label."""
    monkeypatch.setattr(settings, "live_trading", False)  # we are paper…
    sent = _capture_telegram(monkeypatch)
    notify.send("vị thế live", instance="live")  # …but relaying a live reply
    assert sent == ["🔴 LIVE vị thế live"]


# ---------------------------------------------------------------------------
# start() gating on telegram_poll_commands
# ---------------------------------------------------------------------------


def test_start_returns_false_when_poll_commands_off(monkeypatch):
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("bot123:TOKEN"))
    monkeypatch.setattr(settings, "telegram_chat_id", "99999")
    monkeypatch.setattr(settings, "telegram_poll_commands", False)
    assert notify.enabled() is True
    assert notify.start() is False
    assert notify.is_running() is False


# ---------------------------------------------------------------------------
# _split_target()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/pause live", ("live", "/pause")),
        ("/pause paper", ("paper", "/pause")),
        ("/summary", (None, "/summary")),
        ("/summary LIVE", ("live", "/summary")),       # case-insensitive
        ("/fullauto on", (None, "/fullauto on")),       # 'on' is an arg, not a target
        ("/fullauto live on", ("live", "/fullauto on")),
    ],
)
def test_split_target(text, expected):
    assert notify._split_target(text) == expected


# ---------------------------------------------------------------------------
# _proxy_command()
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_proxy_command_unconfigured_sibling(monkeypatch):
    monkeypatch.setattr(settings, "telegram_sibling_url", "")
    out = notify._proxy_command("live", "/summary")
    assert "telegram_sibling_url" in out


def test_proxy_command_relays_and_signs(monkeypatch):
    monkeypatch.setattr(settings, "telegram_sibling_url", "http://127.0.0.1:8001")
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("bot123:TOKEN"))
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResp(200, {"reply": "LIVE summary text"})

    monkeypatch.setattr(notify.httpx, "post", _fake_post)
    out = notify._proxy_command("live", "/summary")
    assert out == "LIVE summary text"
    assert captured["url"] == "http://127.0.0.1:8001/internal/telegram/command"
    assert captured["json"] == {"text": "/summary"}
    expected_sig = hashlib.sha256(b"bot123:TOKEN").hexdigest()
    assert captured["headers"]["X-FM-Internal"] == expected_sig


def test_proxy_command_unreachable_returns_error(monkeypatch):
    monkeypatch.setattr(settings, "telegram_sibling_url", "http://127.0.0.1:8001")
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("bot123:TOKEN"))

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(notify.httpx, "post", _boom)
    out = notify._proxy_command("live", "/summary")
    assert "Không liên lạc được" in out


def test_proxy_command_http_error_status(monkeypatch):
    monkeypatch.setattr(settings, "telegram_sibling_url", "http://127.0.0.1:8001")
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("bot123:TOKEN"))
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: _FakeResp(500, {}))
    out = notify._proxy_command("live", "/summary")
    assert "HTTP 500" in out


# ---------------------------------------------------------------------------
# /internal/telegram/command auth boundary
# ---------------------------------------------------------------------------


def test_internal_endpoint_accepts_valid_signature(monkeypatch):
    from app.routes import InternalCmdBody, internal_telegram_command

    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("bot123:TOKEN"))
    sig = hashlib.sha256(b"bot123:TOKEN").hexdigest()
    out = internal_telegram_command(InternalCmdBody(text="/help"), x_fm_internal=sig)
    assert "/help" in out["reply"]


def test_internal_endpoint_rejects_bad_signature(monkeypatch):
    from fastapi import HTTPException

    from app.routes import InternalCmdBody, internal_telegram_command

    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("bot123:TOKEN"))
    with pytest.raises(HTTPException) as exc:
        internal_telegram_command(InternalCmdBody(text="/help"), x_fm_internal="wrong")
    assert exc.value.status_code == 403


def test_internal_endpoint_closed_when_no_token(monkeypatch):
    """A blank bot token must leave the endpoint closed even to a blank header."""
    from fastapi import HTTPException

    from app.routes import InternalCmdBody, internal_telegram_command

    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr(""))
    with pytest.raises(HTTPException) as exc:
        internal_telegram_command(InternalCmdBody(text="/help"), x_fm_internal="")
    assert exc.value.status_code == 403
