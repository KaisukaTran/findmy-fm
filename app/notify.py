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

from app.clock import utcnow
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


def _telegram_send(text: str, reply_markup: dict | None = None) -> bool:
    """Send *text* to the configured Telegram chat. False on error/disabled.

    ``reply_markup`` (optional) attaches an inline keyboard, e.g.
    ``{"inline_keyboard": [[{"text": "…", "callback_data": "dca:paper:42"}]]}``."""
    if not enabled():
        return False
    try:
        url = f"{_base_url()}/sendMessage"
        payload: dict = {"chat_id": settings.telegram_chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        resp = httpx.post(url, json=payload, timeout=_TIMEOUT)
        return resp.status_code == 200
    except Exception:  # network, timeout, parse error — all swallowed
        logger.debug("notify._telegram_send failed (Telegram unreachable or misconfigured)")
        return False


def _answer_callback(callback_id: str, text: str = "") -> None:
    """Acknowledge a pressed inline button (stops the client spinner; optional toast). Never raises."""
    if not enabled() or not callback_id:
        return
    try:
        httpx.post(f"{_base_url()}/answerCallbackQuery",
                   json={"callback_query_id": callback_id, "text": text[:200]}, timeout=_TIMEOUT)
    except Exception:
        logger.debug("notify._answer_callback failed")


def _edit_message(chat_id: str, message_id: int, text: str) -> None:
    """Replace an alert's text (and drop its buttons) after the action ran. Never raises."""
    if not enabled():
        return
    try:
        httpx.post(f"{_base_url()}/editMessageText",
                   json={"chat_id": chat_id, "message_id": message_id, "text": text},
                   timeout=_TIMEOUT)
    except Exception:
        logger.debug("notify._edit_message failed")


def any_channel_enabled() -> bool:
    """True if at least one alert channel (Telegram or Discord webhook) is configured."""
    if enabled():
        return True
    try:
        from app import notify_discord

        return notify_discord.webhook_enabled()
    except Exception:
        return False


def send(text: str, *, instance: str | None = None, buttons: list | None = None) -> bool:
    """Broadcast *text* to every configured alert channel (Telegram + Discord).

    The message is tagged with an instance label (🧪 PAPER / 🔴 LIVE) so paper and live
    are distinguishable when they share one bot. The tag is THIS instance's by default;
    pass `instance` to label a reply relayed on behalf of the sibling (routed commands).

    ``buttons`` (Telegram only) is a list of rows of ``{"text", "callback_data"}`` dicts —
    an inline keyboard for 1-click actions (Discord gets the plain text, no button).

    Returns True if at least one channel accepted it. Never raises; a failure on one
    channel never suppresses the others.
    """
    text = f"{_label(instance or instance_name())} {text}"
    # Call with 1 arg when there is no keyboard so callers/stubs that predate the reply_markup
    # parameter keep working (backward-compatible signature widening).
    sent = _telegram_send(text, {"inline_keyboard": buttons}) if buttons else _telegram_send(text)
    try:
        from app import notify_discord

        if notify_discord.webhook_enabled():
            sent = notify_discord.send(text) or sent
    except Exception:  # importing/sending to Discord must never break a Telegram alert
        logger.debug("notify: Discord fan-out failed")
    return sent


def _fmt_usd(x: float) -> str:
    """Compact USD with the sign before the $ (-$196, not $-196); no decimals from $100 up."""
    sign, a = ("-" if x < 0 else ""), abs(x)
    return f"{sign}${a:,.0f}" if a >= 100 else f"{sign}${a:,.2f}"


def _fmt_px(x: float) -> str:
    """Price with enough significant digits for sub-cent coins."""
    return f"{x:.6g}"


def _format_maxdca(s: dict) -> str:
    """Human alert body from a service.dca_alert_snapshot dict."""
    lines = [
        f"⛏️ KSS {s['symbol']} — đã DCA hết thang ({s['waves']} sóng)",
        f"📊 Vốn {_fmt_px(s['avg'])} · TT {_fmt_px(s['market'])} · "
        f"uPnL {s['upnl_pct']:+.1f}% ({_fmt_usd(s['upnl_usd'])})",
        f"💰 Đã bơm {_fmt_usd(s['deployed'])} · Sàn SL {_fmt_px(s['sl_floor'])} "
        f"(giá cách sàn {s['room_to_sl_pct']:+.1f}%)",
        f"➕ Nếu thêm sóng {s['next_wave']}: ~{_fmt_usd(s['add_cost'])} @ {_fmt_px(s['add_price'])}",
    ]
    if s["below_sl"]:
        lines.append("⚠️ Rung mới NẰM DƯỚI sàn SL — bấm sẽ bị từ chối (nới SL trước).")
    return "\n".join(lines)


def _maxdca_full_sessions(db) -> list:
    """ACTIVE sessions whose DCA ladder is FULL (no auto rung left) and NOT muted via '✖ Bỏ qua'.
    This is a PULL surface (no proactive push): shown in /summary and listed on demand."""
    from app import runtime
    from app.models import SESSION_ACTIVE, KssSession

    rows = (
        db.query(KssSession)
        .filter(KssSession.status == SESSION_ACTIVE,
                KssSession.current_wave + 1 >= KssSession.max_waves)
        .order_by(KssSession.id)
        .all()
    )
    return [r for r in rows if runtime.get(db, f"maxdca_declined:{r.id}") != "1"]


def maxdca_summary_line(db) -> str:
    """Compact one-liner folded into /summary: how many sessions are full-ladder (need a DCA
    decision) + the first few symbols. Empty string when none or the feature is off."""
    if not settings.telegram_notify_maxdca:
        return ""
    rows = _maxdca_full_sessions(db)
    if not rows:
        return ""
    syms = ", ".join(r.symbol for r in rows[:6]) + ("…" if len(rows) > 6 else "")
    return f"\n🔺 DCA chờ quyết: {len(rows)} session ({syms}) — bấm Liệt kê"


def maxdca_list_button(db=None) -> list | None:
    """The inline '📋 Liệt kê' button attached to a /summary reply — only when full-ladder
    sessions exist. Opens its own DB session if one isn't passed."""
    from app.db import SessionLocal

    close = db is None
    db = db or SessionLocal()
    try:
        if not settings.telegram_notify_maxdca:
            return None
        n = len(_maxdca_full_sessions(db))
        if n == 0:
            return None
        return [[{"text": f"📋 Liệt kê {n} session DCA",
                  "callback_data": f"dcalist:{instance_name()}"}]]
    finally:
        if close:
            db.close()


def send_maxdca_list(db) -> int:
    """ON-DEMAND (pull): send one card per full-ladder session — current state + next-rung cost +
    the '➕ Thêm ~$X' / '✖ Bỏ qua' buttons. Fired when the user taps '📋 Liệt kê' (or /dca_list),
    never on a timer. Returns how many cards were sent."""
    from app.kss import service

    if not enabled():
        return 0
    inst = instance_name()
    sent = 0
    for r in _maxdca_full_sessions(db):
        try:
            snap = service.dca_alert_snapshot(db, r.id)
        except Exception:
            logger.debug("send_maxdca_list: snapshot failed for session %s", r.id)
            continue
        buttons = [[
            {"text": f"➕ Thêm ~{_fmt_usd(snap['add_cost'])}", "callback_data": f"dca:{inst}:{r.id}"},
            {"text": "✖ Bỏ qua", "callback_data": f"dcax:{inst}:{r.id}"},
        ]]
        if send(_format_maxdca(snap), buttons=buttons):
            sent += 1
    return sent


def _reply_buttons(cmd_text: str) -> list | None:
    """Inline buttons to attach to a command reply. /summary gets the '📋 Liệt kê' DCA button
    when any session is full-ladder (the pull entry point — no proactive push)."""
    tok = cmd_text.strip().lstrip("/").split()[0].lower() if cmd_text.strip() else ""
    if tok == "summary":
        return maxdca_list_button()
    return None


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

    Trade/digest pushes are gated by the master telegram_push_enabled switch — when it is off
    (default) the bot stays quiet about routine activity and only replies to commands you send.
    RISK events (SL/breaker/guardian veto) deliberately BYPASS the master mute and fire whenever
    telegram_notify_risk is on (its own kill switch) — a safety alert must never be silenced by a
    convenience flag. Set telegram_notify_risk=False to disable risk pushes entirely.
    """
    if kind == "risk":
        if not settings.telegram_notify_risk:
            return False
    elif not settings.telegram_push_enabled:
        return False
    elif kind == "trade" and not settings.telegram_notify_trades:
        return False
    if throttle_key and not _throttle_ok(f"{kind}:{throttle_key}", cooldown):
        return False
    return send(text)


def fill_alert(fill) -> bool:
    """Push a one-line alert for a Fill. SL/trailing exits route through the *risk* kill
    switch (never throttled); ordinary fills through *trade* (throttled per symbol)."""
    ref = fill.source_ref or ""
    if ref.endswith(":trail_sl"):
        kind, tag = "risk", "📉 Trail-SL"
    elif ref.endswith(":sl"):
        kind, tag = "risk", "🛑 SL"
    elif ref.endswith(":trailing"):
        kind, tag = "risk", "📉 Trailing"
    elif ref.endswith((":tp", ":manual_tp")):
        kind, tag = "trade", "✅ TP"
    elif fill.side == "SELL":
        kind, tag = "trade", "↩️ SELL"
    else:
        kind, tag = "trade", "🟢 BUY"
    pnl = f" · PnL ${fill.realized_pnl:,.2f}" if fill.side == "SELL" else ""
    text = f"{tag} {fill.quantity:g} {fill.symbol} @ {fill.price:g}{pnl}"
    cooldown = 0.0 if kind == "risk" else _TRADE_COOLDOWN
    return event(kind, text, throttle_key=fill.symbol, cooldown=cooldown)


def _recent_skip_count(db, action: str, hours: float = 24.0) -> tuple[int, list[str]]:
    """(count, distinct entities) of an audit action over the last `hours` — for digest monitoring."""
    from datetime import timedelta

    from app.models import AuditLog

    cutoff = utcnow() - timedelta(hours=hours)
    rows = db.query(AuditLog.entity).filter(
        AuditLog.action == action, AuditLog.created_at >= cutoff).all()
    syms: list[str] = []
    for (e,) in rows:
        if e and e not in syms:
            syms.append(e)
    return len(rows), syms[:6]


def build_digest(db) -> str:
    """Compact periodic snapshot: equity, today's realized P&L, all-time realized, open counts."""
    from app import pnlcal, portfolio
    from app.kss import service as kss_service

    s = portfolio.summary_view(db)
    ksum = kss_service.summary(db)
    today = pnlcal.local_today()
    day = pnlcal.realized_by_day(db, today, today).get(today, {}).get("pnl", 0.0)
    # Phase A monitoring: surface how often the relative-strength-vs-BTC gate bit recently — only
    # when the gate is on AND it actually blocked something, so the digest stays clean otherwise.
    rs_line = ""
    if settings.rel_strength_enabled:
        n, syms = _recent_skip_count(db, "skipped_rel_strength", 24.0)
        if n:
            rs_line = (f"\nPhase A (mạnh-hơn-BTC): bỏ {n} lệnh/24h"
                       + (f" — {', '.join(syms)}" if syms else ""))
    return (
        "📈 FINDMY-FM digest\n"
        f"Equity ${s['total_equity']:,.2f}\n"
        f"Hôm nay: ${day:,.2f} · Đã chốt (tổng): ${s['realized_pnl']:,.2f} ({s['realized_pct']:+.2f}%)\n"
        f"Chưa chốt: ${s['unrealized_pnl']:,.2f}\n"
        f"Vị thế {s['positions_count']} · KSS {ksum['active_sessions']} active · "
        f"Pending {s['pending_count']}"
        + rs_line
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


# Max rows a list command prints (Telegram's 4096-char limit fits ~30 lines comfortably). The book
# can hold >15 coins, so the old hard 15-cut silently dropped sizeable positions/sessions.
_TG_LIST_CAP = 30


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
    ) + maxdca_summary_line(db)


def _cmd_pending(db) -> str:
    from app import orders

    pend = orders.list_pending(db, limit=_TG_LIST_CAP)
    if not pend:
        return "Không có lệnh chờ duyệt."
    suffix = "+" if len(pend) >= _TG_LIST_CAP else ""
    lines = [f"⏳ Pending ({len(pend)}{suffix}):"]
    lines += [f"#{o.id} {o.side} {o.quantity:g} {o.symbol} @ {o.price:g}" for o in pend]
    return "\n".join(lines)


def _cmd_positions(db) -> str:
    from app import portfolio

    rows = portfolio.positions_view(db)
    if not rows:
        return "Không có vị thế mở."
    # Biggest-first so the most capital-at-risk always shows; cap high enough to fit a full book
    # (the old rows[:15] in insertion order silently dropped sizeable positions like STG when the
    # book held >15 coins). Telegram's 4096-char limit easily fits ~30 lines.
    rows = sorted(rows, key=lambda r: r.get("market_value", 0.0), reverse=True)
    head = f"📊 Positions ({len(rows)}{', top ' + str(_TG_LIST_CAP) if len(rows) > _TG_LIST_CAP else ''}, lớn nhất trước):"
    lines = [head]
    lines += [
        f"{r['symbol']}: {r['quantity']:g} @ {r['avg_entry_price']:g} · "
        f"uPnL ${r['unrealized_pnl']:,.2f} ({r['unrealized_pnl_pct']:+.1f}%)"
        for r in rows[:_TG_LIST_CAP]
    ]
    return "\n".join(lines)


def _cmd_kss(db) -> str:
    from app.kss import service as kss

    sess = kss.list_sessions(db, limit=_TG_LIST_CAP)
    summ = kss.summary(db)
    if not sess:
        return "Không có phiên KSS."
    lines = [f"🔺 KSS — {summ['active_sessions']}/{summ['total_sessions']} active (≤{_TG_LIST_CAP}):"]
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
    if token in ("dca_add", "dca", "addwave"):
        if not arg.isdigit():
            return "Dùng: /dca_add <session_id> — thêm 1 sóng DCA (cỡ ladder) cho session."
        from app import runtime
        from app.kss import service

        try:
            r = service.queue_manual_extra_wave(db, int(arg))
        except ValueError as exc:
            return f"⚠️ {exc}"
        runtime.set(db, f"maxdca_declined:{arg}", "0")  # a deliberate add re-arms future alerts
        return (f"✅ {r.get('symbol', '?')}: đã thêm sóng {r.get('wave_num', '?')} "
                f"@ {r.get('price', 0):.6g} (~${r.get('cost', 0)}, chờ khớp).")
    if token in ("dca_skip", "dca_no", "skipdca"):
        if not arg.isdigit():
            return "Dùng: /dca_skip <session_id> — bỏ qua, không nhắc thêm DCA cho session này."
        from app import runtime

        runtime.set(db, f"maxdca_declined:{arg}", "1")
        return (f"✖ Đã bỏ qua thêm DCA cho session {arg} — sẽ không nhắc lại "
                f"(bấm /dca_add {arg} để thêm sau).")
    if token in ("dca_list", "dcalist", "dca"):
        n = send_maxdca_list(db)
        return f"📋 Đã gửi {n} thẻ DCA (session full-ladder)." if n else "Không có session full-ladder."
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


def _handle_callback(callback: dict) -> None:
    """Handle a pressed inline button. callback_data is one of:
      ``dcalist:<instance>``    — the /summary '📋 Liệt kê' button: send the detail cards.
      ``dca:<instance>:<sid>``  — a card's '➕' button: add one ladder rung.
      ``dcax:<instance>:<sid>`` — a card's '✖ Bỏ qua' button: mute that session.
    Each maps to a command so it reuses command auth + the paper→live relay (a ``*:live*`` press
    on the paper poller is proxied to live). Same chat_id auth boundary as text commands."""
    data = callback.get("data") or ""
    msg = callback.get("message") or {}
    cb_id = callback.get("id", "")
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != settings.telegram_chat_id:
        return  # AUTH BOUNDARY: ignore presses from any chat but the configured one
    prefix, _, rest = data.partition(":")

    if prefix == "dcalist":
        target = rest or instance_name()
        # '/dca_list' sends the cards itself (locally or on the sibling); we just ack.
        reply = handle_command("/dca_list") if target == instance_name() else _proxy_command(target, "/dca_list")
        _answer_callback(cb_id, reply[:180])
        return

    if prefix not in ("dca", "dcax"):
        _answer_callback(cb_id)
        return
    target, _, sid = rest.partition(":")
    cmd_text = f"/dca_skip {sid}" if prefix == "dcax" else f"/dca_add {sid}"
    reply = handle_command(cmd_text) if target == instance_name() else _proxy_command(target, cmd_text)
    _answer_callback(cb_id, "Đã bỏ qua" if prefix == "dcax" else "Đã xử lý")
    message_id = msg.get("message_id")
    if message_id is not None:
        _edit_message(chat_id, message_id, f"{_label(target)} {reply}")


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

                callback = update.get("callback_query")
                if callback:
                    # Inline-button press (e.g. the max-DCA '+wave' button). Runs the mapped
                    # command off-thread so the poll loop never blocks on DB/network.
                    await asyncio.to_thread(_handle_callback, callback)
                    continue

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
                    send(reply, buttons=_reply_buttons(cmd_text))
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
