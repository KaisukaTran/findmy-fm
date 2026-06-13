"""
Telegram alert sender + remote-control command poller for FINDMY-FM.

Public surface
--------------
send(text)          -- fire-and-forget alert to the configured chat.
handle_command(text)-- parse a Telegram command and return a reply string.
start() / stop() / is_running() -- async background poller lifecycle.

Security boundary
-----------------
The command poller compares every incoming message's chat_id (as a string)
against settings.telegram_chat_id before calling handle_command.  Any update
from an unknown chat is silently dropped.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level poller task (mirrors app/scheduler.py)
# ---------------------------------------------------------------------------

_task: asyncio.Task | None = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_API_BASE = "https://api.telegram.org/bot{token}"
_TIMEOUT = 10.0  # seconds for alert calls
_POLL_TIMEOUT = 30  # long-poll window (seconds) for getUpdates


def enabled() -> bool:
    """Return True iff Telegram integration is fully configured."""
    return (
        settings.telegram_enabled
        and bool(settings.telegram_bot_token.get_secret_value())
        and bool(settings.telegram_chat_id)
    )


def _base_url() -> str:
    """Build the base API URL with the bot token. Never logged."""
    token = settings.telegram_bot_token.get_secret_value()
    return f"https://api.telegram.org/bot{token}"


# ---------------------------------------------------------------------------
# Alert sender
# ---------------------------------------------------------------------------


def send(text: str) -> bool:
    """Send *text* to the configured Telegram chat.

    Returns True on HTTP 200, False on any error or when disabled.
    Never raises; never logs the token.
    """
    if not enabled():
        return False
    try:
        url = f"{_base_url()}/sendMessage"
        payload = {"chat_id": settings.telegram_chat_id, "text": text}
        resp = httpx.post(url, json=payload, timeout=_TIMEOUT)
        return resp.status_code == 200
    except Exception:  # network, timeout, parse error — all swallowed
        logger.debug("notify.send failed (Telegram unreachable or misconfigured)")
        return False


# ---------------------------------------------------------------------------
# Command handler (synchronous; no network calls)
# ---------------------------------------------------------------------------

_HELP_TEXT = (
    "Available commands:\n"
    "  /status  — show automation state + circuit metrics\n"
    "  /pause   — stop full-auto + scheduler\n"
    "  /resume  — start full-auto + scheduler\n"
    "  /freeze  — freeze the circuit breaker (blocks auto-approve)\n"
    "  /reset   — reset (unfreeze) the circuit breaker\n"
    "  /help    — show this message"
)


def handle_command(text: str) -> str:
    """Parse *text* as a Telegram command and return a reply string.

    Opens its own DB session and closes it in a finally block.
    Lazy-imports runtime / circuit / scheduler to avoid import-time cycles.
    """
    # Normalise: strip whitespace, lower-case, drop leading '/', drop @botname
    raw = text.strip()
    if raw.startswith("/"):
        raw = raw[1:]
    token = raw.split()[0].split("@")[0].lower() if raw else ""

    if token in ("help", ""):
        return _HELP_TEXT

    # DB-backed commands
    from app import circuit as _circuit
    from app import runtime as _runtime
    from app import scheduler as _scheduler
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        if token == "status":
            rt = _runtime.state(db)
            sched = _scheduler.is_running()
            cb = _circuit.metrics(db)
            lines = [
                "--- FINDMY-FM status ---",
                f"full_auto:   {rt['full_auto']}",
                f"auto_trade:  {rt['auto_trade']}",
                f"autoapprove: {rt['autoapprove']}",
                f"frozen:      {rt['frozen']}",
                f"frozen_reason: {rt['frozen_reason'] or '-'}",
                f"scheduler:   {'running' if sched else 'stopped'}",
                f"drawdown:    {cb['drawdown_pct']:.2f}%",
                f"daily_loss:  {cb['daily_loss_pct']:.2f}%",
                f"consec_loss: {cb['consecutive_losses']}",
            ]
            return "\n".join(lines)

        if token == "pause":
            _runtime.full_auto_off(db)
            _scheduler.stop()
            return "Paused: full-auto disabled and scheduler stopped."

        if token == "resume":
            _runtime.full_auto_on(db)
            _scheduler.start()
            return "Resumed: full-auto enabled and scheduler started."

        if token == "freeze":
            _runtime.freeze(db, "telegram")
            return "Breaker frozen (auto-approve blocked; manual approval still works)."

        if token == "reset":
            _circuit.reset(db)
            return "Breaker reset: auto-approve unblocked."

        # Unknown command
        return f"Unknown command: /{token}\n\n{_HELP_TEXT}"

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Async command poller
# ---------------------------------------------------------------------------


async def _loop() -> None:
    """Long-poll Telegram getUpdates, dispatch accepted commands.

    Each update is consumed exactly once via the offset mechanism.
    Any update whose chat_id != settings.telegram_chat_id is silently dropped.
    """
    logger.info("notify poller started (interval %ss)", settings.telegram_poll_interval)
    offset: int | None = None

    while True:
        try:
            url = f"{_base_url()}/getUpdates"
            params: dict = {"timeout": _POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset

            async with httpx.AsyncClient(timeout=_POLL_TIMEOUT + _TIMEOUT) as client:
                resp = await client.get(url, params=params)

            if resp.status_code != 200:
                logger.debug("getUpdates returned %s", resp.status_code)
                await asyncio.sleep(settings.telegram_poll_interval)
                continue

            data = resp.json()
            updates = data.get("result", [])

            for update in updates:
                update_id: int = update["update_id"]
                offset = update_id + 1  # advance regardless of outcome

                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue

                incoming_chat_id = str(message.get("chat", {}).get("id", ""))
                # --- AUTH BOUNDARY: only the configured chat may issue commands ---
                if incoming_chat_id != settings.telegram_chat_id:
                    logger.debug(
                        "notify: ignoring update from unknown chat_id %s", incoming_chat_id
                    )
                    continue

                msg_text: str = message.get("text") or ""
                if not msg_text.startswith("/"):
                    continue  # ignore plain messages; only slash-commands are acted on

                try:
                    reply = handle_command(msg_text)
                except Exception:
                    logger.exception("handle_command raised for text=%r", msg_text)
                    reply = "Internal error processing command."

                send(reply)

        except Exception as exc:
            # Log only the type, not the traceback — the bot-token lives in the
            # request URL and rich-traceback loggers could capture it as a local.
            logger.warning("notify poller iteration failed (%s) — will retry", type(exc).__name__)
            await asyncio.sleep(settings.telegram_poll_interval)


# ---------------------------------------------------------------------------
# Lifecycle (mirrors app/scheduler.py)
# ---------------------------------------------------------------------------


def start() -> bool:
    """Start the background command poller if Telegram is enabled and not running.

    Returns True if a new task was created, False if disabled or already running.
    """
    global _task
    if not enabled():
        return False
    if _task and not _task.done():
        return False
    _task = asyncio.create_task(_loop())
    return True


def stop() -> bool:
    """Cancel the background poller. Returns True if a running task was cancelled."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        _task = None
        return True
    _task = None
    return False


def is_running() -> bool:
    """Return True when the poller task is alive."""
    return bool(_task and not _task.done())
