"""
Audit log → meaningful, categorised feed for the dashboard "Nhật ký" tab.

Turns raw (actor, action, entity, detail) rows into a human-readable Vietnamese message
with a category (trade/risk/opus/system), severity, and icon. The write side
(`app.audit.log`) is untouched; this is purely a read/render layer.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app import timefmt
from app.models import AuditLog

# Categories (used as CSS classes cat-<x> + the filter buttons).
TRADE, RISK, OPUS, SYSTEM = "trade", "risk", "opus", "system"


def _detail(row: AuditLog) -> dict:
    if not row.detail:
        return {}
    try:
        d = json.loads(row.detail)
        return d if isinstance(d, dict) else {"value": d}
    except (ValueError, TypeError):
        return {}


def _symbol(row: AuditLog, d: dict) -> str | None:
    sym = d.get("symbol")
    if sym:
        return str(sym)
    ent = row.entity or ""
    # bare-symbol entities (scanner skips log entity=symbol); skip "kss:14"/"order:7" forms.
    if ent and ":" not in ent and ent.isupper():
        return ent
    return None


def _money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def render(row: AuditLog) -> dict:
    """Enrich one AuditLog row → {category, severity, icon, message, symbol, ...}."""
    a, act = row.actor, row.action
    d = _detail(row)
    sym = _symbol(row, d)
    s = sym or ""

    cat, sev, icon, msg = SYSTEM, "info", "•", f"{a} · {act}"

    if act == "session_open":
        cat, sev, icon = TRADE, "good", "🟢"
        msg = f"Mở session KSS {s} ({d.get('mode', 'auto')})"
    elif act == "tp_queued":
        cat, sev, icon = TRADE, "good", "💰"
        msg = f"Chốt lời {s} @ {_money(d.get('price'))} — đưa lệnh bán vào hàng chờ"
    elif act == "stop_queued":
        kind = d.get("kind", "")
        cat, sev, icon = RISK, "danger", "🛑"
        label = "Cắt lỗ" if kind == "stop_loss" else ("Trailing-stop" if kind == "trailing_stop" else "Stop")
        msg = f"{label} {s} @ {_money(d.get('price'))}"
    elif act == "tp_deferred":
        cat, sev, icon = RISK, "warn", "⏸️"
        msg = f"Hoãn chốt lời {s} @ {_money(d.get('price'))} — dưới giá vốn tổng + phí (K-2)"
    elif act == "trailing_deferred":
        cat, sev, icon = RISK, "warn", "⏸️"
        msg = f"Hoãn trailing {s} @ {_money(d.get('price'))} — chỉ chốt khi có lãi (K-trail)"
    elif act == "auto_approve":
        cat, sev, icon = TRADE, "good", "✅"
        msg = f"Tự duyệt lệnh {row.entity or ''}"
    elif a == "guardian" and act == "veto":
        cat, sev, icon = RISK, "danger", "⛔"
        msg = f"Guardian chặn {row.entity or ''}: {d.get('reason', '')}"
    elif act == "guardian_veto":
        cat, sev, icon = RISK, "danger", "⛔"
        msg = f"Guardian chặn lệnh {s} (session {d.get('session', '')})"
    # --- OPUS lifecycle ---
    elif a == "opus" and act == "decide":
        cat, sev, icon = OPUS, "info", "🧠"
        msg = (f"OPUS ra quyết định: {d.get('intents', 0)} ý định"
               f" · cost ${_money(d.get('billed_cost'))}"
               + (" · SHADOW" if d.get("shadow") else ""))
    elif a == "opus" and act == "open":
        cat, sev, icon = TRADE, "good", "🤖"
        msg = f"OPUS mở {s} ${_money(d.get('notional'))} @ {_money(d.get('price'))}"
    elif a == "opus" and act == "close":
        r = d.get("realized", 0) or 0
        cat, sev, icon = OPUS, ("good" if r >= 0 else "danger"), "🤖"
        msg = f"OPUS đóng {s} — đã chốt ${_money(r)}"
    elif a == "opus" and act == "ride":
        cat, sev, icon = OPUS, "good", "🏄"
        msg = f"OPUS giữ ride {s} (thắng sau 3h, uPnL ${_money(d.get('upnl'))})"
    elif a == "opus" and act in ("rescue", "kss_rescue"):
        cat, sev, icon = RISK, "warn", "🆘"
        msg = f"OPUS chuyển {s} sang KSS (rescue — lỗ sau 3h)"
    elif a == "opus" and act == "ride_stop":
        cat, sev, icon = RISK, "danger", "🛑"
        msg = f"OPUS hard-stop ride {s} @ {_money(d.get('price'))}"
    elif a == "opus" and act == "shadow_intent":
        cat, sev, icon = OPUS, "info", "👤"
        msg = f"OPUS (shadow) đề xuất {d.get('intent_action', '')} {s} — không thực thi"
    elif a == "opus" and act.startswith("decide_"):
        cat, sev, icon = OPUS, "warn", "⚠️"
        msg = f"OPUS lỗi khi quyết định ({act})"
    # --- circuit breaker ---
    elif a == "circuit" and act == "freeze":
        cat, sev, icon = RISK, "danger", "🚨"
        msg = f"Circuit-breaker ĐÓNG BĂNG auto: {', '.join(d.get('reasons', [])) or 'ngưỡng rủi ro'}"
    elif a == "circuit" and act == "rearm":
        cat, sev, icon = RISK, "good", "🔓"
        msg = "Circuit-breaker tự gỡ băng (hết cooldown)"
    elif a == "circuit" and act == "reset":
        cat, sev, icon = RISK, "good", "🔓"
        msg = "Circuit-breaker được gỡ băng thủ công"
    # --- system / noise ---
    elif act in ("skipped_cooldown", "skipped_concentration", "skipped_opus_owned", "skipped_cap"):
        reasons = {"skipped_cooldown": "đang cooldown sau stop-loss",
                   "skipped_concentration": "đã đủ session/coin",
                   "skipped_opus_owned": "OPUS đang giữ coin này",
                   "skipped_cap": d.get("reason", "vượt trần vốn")}
        cat, sev, icon = SYSTEM, "info", "⏭️"
        msg = f"Bỏ qua {s}: {reasons.get(act, '')}"
    elif act == "cycle":
        cat, sev, icon = SYSTEM, "info", "⚙️"
        msg = (f"Chu kỳ quét: {d.get('candidates', 0)} ứng viên · "
               f"{d.get('auto_approved', 0)} tự duyệt · {d.get('auto_filled', 0)} khớp"
               + (" · FROZEN" if d.get("frozen") else ""))
    elif act in ("scan_start", "candidate"):
        cat, sev, icon = SYSTEM, "info", "🔎"
        msg = (f"Quét {s} → {d.get('decision', '')}" if act == "candidate"
               else f"Bắt đầu quét ({d.get('universe', '')} coin)")

    return {
        "id": row.id,
        "time": timefmt.local_hms(row.created_at),
        "full_time": timefmt.local_dt(row.created_at),
        "category": cat,
        "severity": sev,
        "icon": icon,
        "message": msg,
        "symbol": sym or "",
        "actor": a,
        "action": act,
        "detail": row.detail or "",
    }


def audit_view(db: Session, limit: int = 300) -> list[dict]:
    """Most-recent enriched audit rows (newest first). Category/symbol filtering is done
    client-side so the 15s poll never loses the active filter."""
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
    return [render(r) for r in rows]
