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
import hashlib
import logging
import time

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
    """Build the base API URL with the bot token. Honours `telegram_api_base` so a
    reverse-proxy (e.g. a Cloudflare Worker) can be used to bypass an SNI block. Never logged."""
    token = settings.telegram_bot_token.get_secret_value()
    base = settings.telegram_api_base.rstrip("/")
    return f"{base}/bot{token}"


# ---------------------------------------------------------------------------
# Instance identity (paper vs live) — for labelling alerts + command routing
# ---------------------------------------------------------------------------

_INSTANCES = ("paper", "live")
_LABELS = {"live": "[LIVE]", "paper": "[PAPER]"}


def instance_name() -> str:
    """'live' or 'paper' for THIS instance, derived from settings.live_trading.

    Both instances may share one bot; this tag tells paper and live apart in every
    outbound message and is the target keyword for routed commands ('/pause live')."""
    return "live" if settings.live_trading else "paper"


def _label(name: str) -> str:
    """The chat-visible tag for an instance name (falls back to upper-case)."""
    return _LABELS.get(name, name.upper())


def _internal_signature() -> str:
    """Shared secret for the cross-instance command endpoint: sha256 of the bot token.

    Both instances share the same bot token (one bot), so this proves a caller is a
    sibling without any extra config. Empty token → empty string (endpoint stays closed)."""
    token = settings.telegram_bot_token.get_secret_value()
    return hashlib.sha256(token.encode()).hexdigest() if token else ""


# ---------------------------------------------------------------------------
# Alert sender
# ---------------------------------------------------------------------------


def _telegram_send(text: str) -> bool:
    """Send *text* to the configured Telegram chat. False on error/disabled."""
    if not enabled():
        return False
    try:
        url = f"{_base_url()}/sendMessage"
        payload = {"chat_id": settings.telegram_chat_id, "text": text}
        resp = httpx.post(url, json=payload, timeout=_TIMEOUT)
        return resp.status_code == 200
    except Exception:  # network, timeout, parse error — all swallowed
        logger.debug("notify._telegram_send failed (Telegram unreachable or misconfigured)")
        return False


def any_channel_enabled() -> bool:
    """True if at least one alert channel (Telegram or Discord webhook) is configured."""
    if enabled():
        return True
    try:
        from app import notify_discord

        return notify_discord.webhook_enabled()
    except Exception:
        return False


def send(text: str, *, instance: str | None = None) -> bool:
    """Broadcast *text* to every configured alert channel (Telegram + Discord).

    The message is tagged with an instance label (🧪 PAPER / 🔴 LIVE) so paper and live
    are distinguishable when they share one bot. The tag is THIS instance's by default;
    pass `instance` to label a reply relayed on behalf of the sibling (routed commands).

    Returns True if at least one channel accepted it. Never raises; a failure on one
    channel never suppresses the others.
    """
    text = f"{_label(instance or instance_name())} {text}"
    sent = _telegram_send(text)
    try:
        from app import notify_discord

        if notify_discord.webhook_enabled():
            sent = notify_discord.send(text) or sent
    except Exception:  # importing/sending to Discord must never break a Telegram alert
        logger.debug("notify: Discord fan-out failed")
    return sent


# ---------------------------------------------------------------------------
# Categorised event push (kill switches + per-key throttle)
# ---------------------------------------------------------------------------

# Per-key last-sent monotonic timestamps for throttling (e.g. one key per symbol).
_last_event: dict[str, float] = {}
_TRADE_COOLDOWN = 8.0  # seconds; coalesce a chatty same-symbol DCA wave into one trade push


def _throttle_ok(key: str, cooldown: float) -> bool:
    """True if `key` hasn't fired within `cooldown` seconds (and stamps it). cooldown<=0 = always."""
    if cooldown <= 0:
        return True
    now = time.monotonic()
    if now - _last_event.get(key, 0.0) < cooldown:
        return False
    _last_event[key] = now
    return True


def event(kind: str, text: str, *, throttle_key: str | None = None, cooldown: float = 0.0) -> bool:
    """Push an alert for a category, honouring its kill switch + optional per-key throttle.

    kind="trade" → gated by telegram_notify_trades; kind="risk" → telegram_notify_risk.
    Risk events are never throttled (an SL/breaker alert must always go out).

    All proactive pushes are first gated by the master telegram_push_enabled switch — when
    it is off (default) the bot stays silent and only replies to commands you send.
    """
    if not settings.telegram_push_enabled:
        return False
    if kind == "trade" and not settings.telegram_notify_trades:
        return False
    if kind == "risk" and not settings.telegram_notify_risk:
        return False
    if throttle_key and not _throttle_ok(f"{kind}:{throttle_key}", cooldown):
        return False
    return send(text)


def fill_alert(fill) -> bool:
    """Push a one-line alert for a Fill. SL/trailing exits route through the *risk* kill
    switch (never throttled); ordinary fills through *trade* (throttled per symbol)."""
    ref = fill.source_ref or ""
    if ref.endswith(":sl"):
        kind, tag = "risk", "🛑 SL"
    elif ref.endswith(":trailing"):
        kind, tag = "risk", "📉 Trailing"
    elif ref.endswith(":tp"):
        kind, tag = "trade", "✅ TP"
    elif fill.side == "SELL":
        kind, tag = "trade", "↩️ SELL"
    else:
        kind, tag = "trade", "🟢 BUY"
    pnl = f" · PnL ${fill.realized_pnl:,.2f}" if fill.side == "SELL" else ""
    text = f"{tag} {fill.quantity:g} {fill.symbol} @ {fill.price:g}{pnl}"
    cooldown = 0.0 if kind == "risk" else _TRADE_COOLDOWN
    return event(kind, text, throttle_key=fill.symbol, cooldown=cooldown)


def build_digest(db) -> str:
    """Compact periodic snapshot: equity, today's realized P&L, all-time realized, open counts."""
    from app import pnlcal, portfolio
    from app.kss import service as kss_service

    s = portfolio.summary_view(db)
    ksum = kss_service.summary(db)
    today = pnlcal.local_today()
    day = pnlcal.realized_by_day(db, today, today).get(today, {}).get("pnl", 0.0)
    return (
        "📈 FINDMY-FM digest\n"
        f"Equity ${s['total_equity']:,.2f}\n"
        f"Hôm nay: ${day:,.2f} · Đã chốt (tổng): ${s['realized_pnl']:,.2f} ({s['realized_pct']:+.2f}%)\n"
        f"Chưa chốt: ${s['unrealized_pnl']:,.2f}\n"
        f"Vị thế {s['positions_count']} · KSS {ksum['active_sessions']} active · "
        f"Pending {s['pending_count']}"
    )


def maybe_send_digest(db) -> bool:
    """Push a digest if `telegram_digest_hours` has elapsed since the last one. No-op when
    proactive push is off (master switch), the interval is 0, or Telegram off. Tracks the
    last send in-process."""
    if not settings.telegram_push_enabled:
        return False
    hours = settings.telegram_digest_hours
    if hours <= 0 or not any_channel_enabled():
        return False
    if not _throttle_ok("digest", hours * 3600.0):
        return False
    return send(build_digest(db))


# ---------------------------------------------------------------------------
# Command handler (synchronous; no network calls)
# ---------------------------------------------------------------------------

_HELP_TEXT = (
    "Lệnh khả dụng:\n"
    "  /summary   — equity, cash, P&L (đã/chưa chốt)\n"
    "  /status    — automation + chỉ số breaker\n"
    "  /pending   — lệnh chờ duyệt\n"
    "  /positions — vị thế đang mở\n"
    "  /kss       — phiên KSS\n"
    "  /trade [N|buy|sell] — giao dịch gần nhất (mặc định 10; lọc buy/sell)\n"
    "  /fullauto on|off — bật/tắt Full-Auto\n"
    "  /pause     — tắt Full-Auto + scheduler\n"
    "  /resume    — bật Full-Auto + scheduler\n"
    "  /freeze    — đóng băng breaker (chặn auto-approve)\n"
    "  /reset     — mở băng breaker\n"
    "  /help      — hiện trợ giúp\n"
    "Thêm 'paper' hoặc 'live' ngay sau lệnh để chọn instance, vd: /summary live, /pause live."
)


def _cmd_summary(db) -> str:
    from app import portfolio

    s = portfolio.summary_view(db)
    return (
        "💰 FINDMY-FM — Tổng quan\n"
        f"Equity:     ${s['total_equity']:,.2f}\n"
        f"Cash:       ${s['cash']:,.2f} ({s['cash_pct']:.0f}%)\n"
        f"Market val: ${s['total_market_value']:,.2f}\n"
        f"Realized:   ${s['realized_pnl']:,.2f} ({s['realized_pct']:+.2f}%)\n"
        f"Unrealized: ${s['unrealized_pnl']:,.2f} ({s['unrealized_pct']:+.2f}%)\n"
        f"Trades {s['total_trades']} · Pending {s['pending_count']} · "
        f"Positions {s['positions_count']}"
    )


def _cmd_pending(db) -> str:
    from app import orders

    pend = orders.list_pending(db, limit=15)
    if not pend:
        return "Không có lệnh chờ duyệt."
    lines = ["⏳ Pending (≤15):"]
    lines += [f"#{o.id} {o.side} {o.quantity:g} {o.symbol} @ {o.price:g}" for o in pend]
    return "\n".join(lines)


def _cmd_positions(db) -> str:
    from app import portfolio

    rows = portfolio.positions_view(db)
    if not rows:
        return "Không có vị thế mở."
    lines = ["📊 Positions (≤15):"]
    lines += [
        f"{r['symbol']}: {r['quantity']:g} @ {r['avg_entry_price']:g} · "
        f"uPnL ${r['unrealized_pnl']:,.2f} ({r['unrealized_pnl_pct']:+.1f}%)"
        for r in rows[:15]
    ]
    return "\n".join(lines)


def _cmd_kss(db) -> str:
    from app.kss import service as kss

    sess = kss.list_sessions(db, limit=15)
    summ = kss.summary(db)
    if not sess:
        return "Không có phiên KSS."
    lines = [f"🔺 KSS — {summ['active_sessions']}/{summ['total_sessions']} active (≤15):"]
    for s in sess:
        mode = (f"🔼trailing-TP SL={s.get('trail_sl_price') or 0.0:g}"
                if s.get("trail_active") else f"DCA {s.get('filled_waves_count', 0)}/{s.get('max_waves', 0)}")
        lines.append(
            f"{s.get('symbol')} [{s.get('status')}] "
            f"avg {s.get('avg_price') or 0.0:g} · now {s.get('current_price') or 0.0:g} · {mode}"
        )
    return "\n".join(lines)


def _cmd_trade(db, arg: str = "") -> str:
    """Recent trades. Optional arg: a count (``/trade 20``, capped 1..50) OR a side filter
    (``/trade buy`` / ``/trade sell``). Bare ``/trade`` = 10 most recent, both sides."""
    from app import portfolio, timefmt

    side: str | None = None
    limit = 10
    a = (arg or "").strip().lower()
    if a in ("buy", "sell"):
        side = a.upper()
    elif a.isdigit():
        limit = max(1, min(int(a), 50))
    rows = portfolio.trades_view(db, limit=limit, side=side)
    if not rows:
        return f"Chưa có lệnh {side}." if side else "Chưa có giao dịch nào."
    lines = [f"🧾 Trades{(' ' + side) if side else ''} ({len(rows)} gần nhất):"]
    for r in rows:
        pnl = f" · pnl ${r['realized_pnl']:,.2f}" if r["side"] == "SELL" else ""
        lines.append(
            f"{timefmt.local_hms(r['executed_at'])} {r['symbol']} {r['side']} "
            f"{r['quantity']:g} @ {r['price']:g}{pnl} [{r['source']}]"
        )
    return "\n".join(lines)


def _cmd_status(db) -> str:
    from app import circuit, runtime, scheduler

    rt, cb = runtime.state(db), circuit.metrics(db)
    return "\n".join([
        "--- FINDMY-FM status ---",
        f"full_auto:   {rt['full_auto']}",
        f"auto_trade:  {rt['auto_trade']}",
        f"autoapprove: {rt['autoapprove']}",
        f"frozen:      {rt['frozen']}",
        f"frozen_reason: {rt['frozen_reason'] or '-'}",
        f"scheduler:   {'running' if scheduler.is_running() else 'stopped'}",
        f"drawdown:    {cb['drawdown_pct']:.2f}%",
        f"daily_loss:  {cb['daily_loss_pct']:.2f}%",
        f"consec_loss: {cb['consecutive_losses']}",
    ])


# token -> read-only handler (state-changing commands live in _handle_control)
_INFO_COMMANDS = {
    "summary": _cmd_summary,
    "pending": _cmd_pending,
    "positions": _cmd_positions,
    "position": _cmd_positions,
    "pos": _cmd_positions,
    "kss": _cmd_kss,
    "trade": _cmd_trade,
    "trades": _cmd_trade,
    "status": _cmd_status,
}


def _handle_info(db, token: str, arg: str = "") -> str | None:
    """Dispatch a read-only info command, or None if `token` isn't one. Only /trade consumes
    `arg` (count or buy/sell filter); the rest ignore it."""
    handler = _INFO_COMMANDS.get(token)
    if handler is None:
        return None
    return handler(db, arg) if handler is _cmd_trade else handler(db)


def _set_full_auto(db, on: bool) -> None:
    """Toggle full-auto + the scheduler loop together (shared by /resume /pause /fullauto)."""
    from app import runtime, scheduler

    (runtime.full_auto_on if on else runtime.full_auto_off)(db)
    (scheduler.start if on else scheduler.stop)()


def _handle_control(db, token: str, arg: str) -> str:
    """Control commands (state-changing). Returns a reply for any token (unknown → help)."""
    if token == "pause":
        _set_full_auto(db, False)
        return "Paused: full-auto disabled and scheduler stopped."
    if token == "resume":
        _set_full_auto(db, True)
        return "Resumed: full-auto enabled and scheduler started."
    if token == "fullauto":
        if arg not in ("on", "off"):
            return "Dùng: /fullauto on  hoặc  /fullauto off"
        on = arg == "on"
        _set_full_auto(db, on)
        state = "ON (scheduler đã chạy)" if on else "OFF (scheduler đã dừng)"
        return f"Full-Auto: {state}."
    if token == "freeze":
        from app import runtime

        runtime.freeze(db, "telegram")
        return "Breaker frozen (auto-approve blocked; manual approval still works)."
    if token == "reset":
        from app import circuit

        circuit.reset(db)
        return "Breaker reset: auto-approve unblocked."
    return f"Unknown command: /{token}\n\n{_HELP_TEXT}"


def handle_command(text: str) -> str:
    """Parse *text* as a Telegram command and return a reply string.

    Opens its own DB session and closes it in a finally block. Dispatches to
    `_handle_info` (read-only) then `_handle_control` (state-changing).
    """
    raw = text.strip()
    if raw.startswith("/"):
        raw = raw[1:]
    parts = raw.split()
    token = parts[0].split("@")[0].lower() if parts else ""
    arg = parts[1].lower() if len(parts) > 1 else ""

    if token in ("help", ""):
        return _HELP_TEXT

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        reply = _handle_info(db, token, arg)
        return reply if reply is not None else _handle_control(db, token, arg)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Cross-instance command routing (paper polls; '/pause live' reaches the sibling)
# ---------------------------------------------------------------------------


def _split_target(text: str) -> tuple[str | None, str]:
    """Pull an optional instance target ('paper'/'live') sitting right after the command.

    '/pause live'      -> ('live', '/pause')
    '/summary'         -> (None,   '/summary')
    '/fullauto on'     -> (None,   '/fullauto on')      ('on' is an arg, not a target)
    '/fullauto live on'-> ('live', '/fullauto on')
    """
    parts = text.split()
    if len(parts) >= 2 and parts[1].lower() in _INSTANCES:
        return parts[1].lower(), " ".join([parts[0], *parts[2:]])
    return None, text


def _proxy_command(target: str, cmd_text: str) -> str:
    """Run *cmd_text* on the sibling instance via its internal endpoint and return its raw
    reply (the caller labels it with *target*'s tag). Never raises — returns an error string."""
    base = settings.telegram_sibling_url.rstrip("/")
    if not base:
        return f"Chưa cấu hình telegram_sibling_url → không định tuyến được tới '{target}'."
    try:
        resp = httpx.post(
            f"{base}/internal/telegram/command",
            json={"text": cmd_text},
            headers={"X-FM-Internal": _internal_signature()},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return f"Instance '{target}' trả lỗi HTTP {resp.status_code}."
        return resp.json().get("reply") or "(instance không trả nội dung)"
    except Exception:
        logger.warning("notify: proxy command to %s failed (sibling unreachable)", target)
        return f"Không liên lạc được instance '{target}'."


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

                target, cmd_text = _split_target(msg_text)
                if target is None or target == instance_name():
                    # Command for THIS instance: handle locally, reply with our own label.
                    try:
                        reply = handle_command(cmd_text)
                    except Exception:
                        logger.exception("handle_command raised for text=%r", cmd_text)
                        reply = "Internal error processing command."
                    send(reply)
                else:
                    # Command targets the sibling: relay over localhost, label the reply
                    # with the TARGET's tag (off-thread so the poll loop never blocks).
                    reply = await asyncio.to_thread(_proxy_command, target, cmd_text)
                    send(reply, instance=target)

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

    Returns True if a new task was created, False if disabled, already running, or this
    instance is configured alerts-only (telegram_poll_commands=false) — the latter lets a
    secondary instance (e.g. live) push labelled alerts without stealing the command stream.
    """
    global _task
    if not enabled():
        return False
    if not settings.telegram_poll_commands:
        logger.info("notify: command poller disabled here (telegram_poll_commands=false); alerts-only")
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
