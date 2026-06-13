"""
Discord notifier for FINDMY-FM — alternative alert + remote-control channel.

Why this exists
---------------
This machine's network blocks Telegram at the TLS/SNI layer (TCP connects, the
handshake to api.telegram.org is dropped). Discord is reachable, so it serves as
the alert + command channel when Telegram can't.

Public surface (mirrors app/notify.py)
--------------------------------------
send(text)            -- fire-and-forget alert to the configured channel webhook.
webhook_enabled()     -- True iff a channel webhook is configured (push side).
command_enabled()     -- True iff a bot token + channel id are configured (2-way side).
start()/stop()/is_running() -- async command-gateway lifecycle.

The command bot reuses app.notify.handle_command (transport-agnostic), so the
exact same /summary /pending /pause ... commands work on Discord.

Security boundary
-----------------
The gateway only acts on messages whose channel_id == settings.discord_channel_id
and ignores bot authors. Everything else is dropped. The bot token lives only in
request headers / the IDENTIFY payload and is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None

_TIMEOUT = 10.0  # seconds for webhook/REST calls
_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
_REST_BASE = "https://discord.com/api/v10"
_RECONNECT_DELAY = 5.0  # seconds between gateway reconnect attempts

# Gateway intents: GUILD_MESSAGES (1<<9) + DIRECT_MESSAGES (1<<12) + MESSAGE_CONTENT (1<<15).
# MESSAGE_CONTENT is PRIVILEGED — it must be toggled on in the Developer Portal or the
# gateway closes the connection (op 4014). Without it, message.content arrives empty.
_INTENTS = (1 << 9) | (1 << 12) | (1 << 15)


# ---------------------------------------------------------------------------
# Enablement
# ---------------------------------------------------------------------------


def webhook_enabled() -> bool:
    """True iff Discord push (channel webhook) is configured."""
    return settings.discord_enabled and bool(settings.discord_webhook_url.get_secret_value())


def command_enabled() -> bool:
    """True iff the 2-way command gateway (bot token + channel id) is configured."""
    return (
        settings.discord_enabled
        and bool(settings.discord_bot_token.get_secret_value())
        and bool(settings.discord_channel_id)
    )


# ---------------------------------------------------------------------------
# Alert sender (webhook)
# ---------------------------------------------------------------------------


def send(text: str) -> bool:
    """Push *text* to the configured Discord channel webhook.

    Returns True on HTTP 200/204, False on any error or when disabled. Never raises;
    never logs the webhook URL (it embeds a secret token).
    """
    if not webhook_enabled():
        return False
    try:
        url = settings.discord_webhook_url.get_secret_value()
        # Discord rejects empty content and caps a message at 2000 chars.
        resp = httpx.post(url, json={"content": (text or "")[:2000]}, timeout=_TIMEOUT)
        return resp.status_code in (200, 204)
    except Exception:  # network/timeout/parse — all swallowed
        logger.debug("notify_discord.send failed (Discord unreachable or misconfigured)")
        return False


def _reply(channel_id: str, text: str) -> None:
    """Post a command reply back to the channel via the bot REST API (best-effort)."""
    token = settings.discord_bot_token.get_secret_value()
    try:
        httpx.post(
            f"{_REST_BASE}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": (text or "")[:2000]},
            timeout=_TIMEOUT,
        )
    except Exception:
        logger.debug("notify_discord reply failed")


# ---------------------------------------------------------------------------
# Command gateway (2-way)
# ---------------------------------------------------------------------------


def _dispatch_message(data: dict) -> None:
    """Handle one MESSAGE_CREATE payload: auth-gate, then run the command + reply."""
    if (data.get("author") or {}).get("bot"):
        return  # never react to bot messages (including our own) — avoids loops
    channel_id = str(data.get("channel_id", ""))
    # --- AUTH BOUNDARY: only the configured channel may issue commands ---
    if channel_id != settings.discord_channel_id:
        return
    content = data.get("content") or ""
    if not content.startswith("/"):
        return  # only slash-commands are acted on
    from app import notify

    try:
        reply = notify.handle_command(content)
    except Exception:
        logger.exception("discord handle_command raised for %r", content)
        reply = "Internal error processing command."
    _reply(channel_id, reply)


async def _heartbeat(ws, interval: float, state: dict) -> None:
    """Send op-1 heartbeats every `interval` seconds with the last sequence number."""
    while True:
        await asyncio.sleep(interval)
        await ws.send(json.dumps({"op": 1, "d": state.get("seq")}))


async def _run_connection() -> None:
    """Open one gateway session: HELLO → IDENTIFY → dispatch loop. Returns on close."""
    import websockets

    token = settings.discord_bot_token.get_secret_value()
    async with websockets.connect(_GATEWAY_URL, max_size=2**20) as ws:
        hello = json.loads(await ws.recv())
        interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
        await ws.send(
            json.dumps(
                {
                    "op": 2,
                    "d": {
                        "token": token,
                        "intents": _INTENTS,
                        "properties": {"os": "linux", "browser": "findmy-fm", "device": "findmy-fm"},
                    },
                }
            )
        )
        state: dict = {"seq": None}
        hb = asyncio.create_task(_heartbeat(ws, interval, state))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("s") is not None:
                    state["seq"] = msg["s"]
                op = msg.get("op")
                if op == 0:  # dispatch
                    if msg.get("t") == "MESSAGE_CREATE":
                        _dispatch_message(msg.get("d") or {})
                elif op == 1:  # server requested an immediate heartbeat
                    await ws.send(json.dumps({"op": 1, "d": state.get("seq")}))
                elif op in (7, 9):  # reconnect / invalid session → drop and re-IDENTIFY
                    break
        finally:
            hb.cancel()


async def _loop() -> None:
    """Keep a gateway session alive, reconnecting with a fixed backoff on any error."""
    logger.info("discord command gateway started")
    while True:
        try:
            await _run_connection()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Log only the type — the bot token lives in the IDENTIFY payload and a
            # rich traceback logger could capture it as a local.
            logger.warning("discord gateway iteration failed (%s) — will retry", type(exc).__name__)
        await asyncio.sleep(_RECONNECT_DELAY)


# ---------------------------------------------------------------------------
# Lifecycle (mirrors app/notify.py)
# ---------------------------------------------------------------------------


def start() -> bool:
    """Start the command gateway if 2-way is configured and not already running.

    Returns True if a new task was created, False if commands are off or already running.
    (Webhook push needs no task — it is a stateless POST per alert.)
    """
    global _task
    if not command_enabled():
        return False
    if _task and not _task.done():
        return False
    _task = asyncio.create_task(_loop())
    return True


def stop() -> bool:
    """Cancel the command gateway. Returns True if a running task was cancelled."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        _task = None
        return True
    _task = None
    return False


def is_running() -> bool:
    """Return True when the gateway task is alive."""
    return bool(_task and not _task.done())
