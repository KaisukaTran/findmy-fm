"""Scanner funnel — "why isn't the bot opening trades?" rollup for the
Chiến lược landing tab (docs/ui-rebuild-brief.md §5.1.1, the P3 funnel spec
handed to this phase).

Read-only reporting layer: rolls up ``ScanRun`` + ``AuditLog`` (+ one
``Candidate.decision='trade'`` count) that ``app/scanner.py`` already writes.
It does NOT count from ``candidates.decision`` for the late blocking branches
(Grok veto, per-scan cap, per-symbol/portfolio cap) — those never flip a
candidate's ``decision`` back to 'skip' once the initial gate said 'trade', so
that column would undercount real rejections by ~60x. Every row below (other
than "Đạt trade", which the initial score gate DOES set correctly) is counted
from the named ``audit_log`` action instead — the single source of truth for
"why did this get blocked". This module never touches scanner logic, only
SELECTs against tables it already writes.

EXT-4 (docs/ui-rebuild-brief.md): the pipeline row list lives in ONE place
(``_STAGES``) so a new stage can be added without touching the template.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import Settings, settings
from app.models import AuditLog, Candidate, ScanRun

# Poll window choices exposed on the partial (T2 tier — see docs/ui-rebuild-brief.md §9).
WINDOWS: dict[str, int] = {"24h": 24, "7d": 24 * 7}

# scanner.py:42 — data-thin cutoff (candle count). Read-only mirror for the "Ghi chú"
# column; NOT imported from scanner.py to avoid coupling this reporting module to
# scanner internals (it's a stable literal, not a runtime setting).
_MIN_CANDLES = 30


def _note_scan_runs(s: Settings) -> str:
    return f"quét mỗi {s.scan_interval_min:.0f} phút"


def _note_budget_skip(s: Settings) -> str:
    return f"giữ {s.equity_backup_pct:.0f}% vốn dự phòng (equity_backup_pct)"


def _note_max_concurrent_skip(s: Settings) -> str:
    return f"tối đa {s.max_concurrent_sessions} phiên song song (max_concurrent_sessions)"


def _note_thin_data(s: Settings) -> str:
    return f"< {_MIN_CANDLES} nến"


def _note_downtrend(s: Settings) -> str:
    return f"ADX ≥ {s.block_downtrend_adx:.0f} (block_downtrend_adx)" if s.block_downtrend_adx > 0 else "TẮT"


def _note_entry_timing(s: Settings) -> str:
    bits = [f"momentum gate {'BẬT' if s.entry_momentum_gate else 'TẮT'}"]
    if s.max_avg_mae_pct > 0:
        bits.append(f"MAE TB > {s.max_avg_mae_pct:.0f}% (max_avg_mae_pct)")
    return " · ".join(bits)


def _note_rel_strength(s: Settings) -> str:
    if not s.rel_strength_enabled:
        return "TẮT (rel_strength_enabled)"
    return f"lệch > {s.rel_strength_margin_pct:.1f}% so BTC (rel_strength_margin_pct)"


def _note_cooldown(s: Settings) -> str:
    return f"{s.stop_cooldown_min:.0f} phút sau stop (stop_cooldown_min)" if s.stop_cooldown_min > 0 else "TẮT"


def _note_loss_reentry(s: Settings) -> str:
    if not s.loss_reentry_enabled:
        return "TẮT (loss_reentry_enabled)"
    return f"{s.loss_reentry_weeks_1}w / {s.loss_reentry_weeks_2}w (loss_reentry_weeks_1/_2)"


def _note_loss_streak(s: Settings) -> str:
    return f"cửa sổ {s.loss_streak_window_days} ngày (loss_streak_window_days)"


def _note_concentration(s: Settings) -> str:
    if s.max_sessions_per_symbol <= 0:
        return "TẮT"
    return f"tối đa {s.max_sessions_per_symbol} phiên/coin (max_sessions_per_symbol)"


def _note_mae_quartile(s: Settings) -> str:
    return "BẬT — loại quartile sâu nhất (mae_quartile_gate_enabled)" if s.mae_quartile_gate_enabled else "TẮT"


def _note_grok_veto(s: Settings) -> str:
    return "Grok (xAI) thẩm định rủi ro" if s.grok_scanner_enabled else "TẮT (grok_scanner_enabled)"


def _note_per_scan_cap(s: Settings) -> str:
    if not s.max_new_sessions_per_scan:
        return "TẮT (max_new_sessions_per_scan=0)"
    return f"tối đa {s.max_new_sessions_per_scan} phiên MỚI/lần quét (max_new_sessions_per_scan)"


def _note_skipped_cap(s: Settings) -> str:
    return (f"giữ {s.equity_backup_pct:.0f}% dự phòng · tối đa {s.max_concurrent_sessions} phiên "
            f"(equity_backup_pct / max_concurrent_sessions)")


def _no_note(_s: Settings) -> str | None:
    return None


# key: internal id (used for the warn flag + tests). label: Vietnamese column 1.
# source: "runs" (ScanRun count), "budget_skip"/"max_concurrent_skip" (the
# scan_skipped detail-reason split), "trade" (Candidate.decision='trade'), or the
# audit_log `action` string to COUNT(*) in-window (optionally scoped by `actor`).
# base: which denominator "% còn lại" divides by.
_STAGES: list[dict] = [
    dict(key="scan_runs", label="Chu kỳ quét đã chạy", source="runs",
         base="runs", note=_note_scan_runs),
    dict(key="scan_skipped_budget", label="Bỏ CẢ chu kỳ — vượt ngân sách", source="budget_skip",
         base="runs", note=_note_budget_skip),
    dict(key="scan_skipped_max_concurrent", label="Bỏ CẢ chu kỳ — max concurrent",
         source="max_concurrent_skip", base="runs", note=_note_max_concurrent_skip),
    dict(key="candidate", label="Ứng viên đã chấm", source="candidate",
         base="candidate", note=_no_note),
    dict(key="skipped_thin_data", label="Loại: dữ liệu mỏng", source="skipped_thin_data",
         base="candidate", note=_note_thin_data),
    dict(key="skipped_downtrend", label="Loại: downtrend xác nhận", source="skipped_downtrend",
         base="candidate", note=_note_downtrend),
    dict(key="skipped_entry_timing", label="Loại: entry timing (dao rơi / MAE)",
         source="skipped_entry_timing", base="candidate", note=_note_entry_timing),
    dict(key="skipped_rel_strength", label="Loại: yếu hơn BTC (rel-strength)",
         source="skipped_rel_strength", base="candidate", note=_note_rel_strength),
    dict(key="trade", label='Đạt "trade"', source="trade", base="candidate", note=_no_note),
    dict(key="skipped_cooldown", label="Chặn: cooldown sau stop", source="skipped_cooldown",
         base="candidate", note=_note_cooldown),
    dict(key="skipped_pending_sell", label="Chặn: đang có lệnh SELL treo",
         source="skipped_pending_sell", base="candidate", note=_no_note),
    dict(key="skipped_loss_reentry", label="Chặn: tái vào sau lỗ", source="skipped_loss_reentry",
         base="candidate", note=_note_loss_reentry),
    dict(key="skipped_loss_streak", label="Chặn: chuỗi thua", source="skipped_loss_streak",
         base="candidate", note=_note_loss_streak),
    dict(key="skipped_concentration", label="Chặn: trùng coin (K-1)", source="skipped_concentration",
         base="candidate", note=_note_concentration),
    dict(key="skipped_mae_quartile", label="Chặn: MAE quartile", source="skipped_mae_quartile",
         base="candidate", note=_note_mae_quartile),
    dict(key="scanner_veto", label="Chặn: Grok veto", source="scanner_veto",
         base="candidate", note=_note_grok_veto, actor="grok"),
    dict(key="skipped_per_scan_cap", label="Chặn: trần lệnh/lần quét", source="skipped_per_scan_cap",
         base="candidate", note=_note_per_scan_cap),
    dict(key="skipped_cap", label="Chặn: ngân sách / concurrency (theo symbol)",
         source="skipped_cap", base="candidate", note=_note_skipped_cap),
    dict(key="session_open", label="Đã mở phiên", source="session_open",
         base="candidate", note=_no_note),
]

# The plain audit_log actions rolled up in the one GROUP BY query below.
_AUDIT_ACTIONS = [
    s["source"] for s in _STAGES
    if s["source"] not in ("runs", "budget_skip", "max_concurrent_skip", "trade")
]


def _scan_skipped_split(db: Session, since) -> tuple[int, int]:
    """(budget_skips, max_concurrent_skips) — the ONE place this module reads the
    JSON `detail` blob, and only for the (small) scan_skipped subset of a window
    (bounded by how many whole cycles got skipped, never the full audit table)."""
    budget = max_conc = 0
    rows = (
        db.query(AuditLog.detail)
        .filter(AuditLog.created_at >= since, AuditLog.action == "scan_skipped")
        .all()
    )
    for (detail,) in rows:
        reason = ""
        if detail:
            try:
                reason = json.loads(detail).get("reason", "") or ""
            except (ValueError, TypeError):
                reason = ""
        if reason.startswith("max concurrent"):
            max_conc += 1
        else:
            budget += 1
    return budget, max_conc


def funnel_view(db: Session, window: str = "24h") -> dict:
    """Rollup for one trailing window ("24h" or "7d"). Cheap: one GROUP BY over
    audit_log (indexed on created_at) + 3 small COUNT()s + the bounded
    scan_skipped detail split — mirrors app/auditview.py:recent_by_category's
    windowing pattern. Never a per-row full-table scan."""
    hours = WINDOWS.get(window, WINDOWS["24h"])
    since = utcnow() - timedelta(hours=hours)

    audit_counts = dict(
        db.query(AuditLog.action, func.count(AuditLog.id))
        .filter(AuditLog.created_at >= since, AuditLog.action.in_(_AUDIT_ACTIONS))
        .group_by(AuditLog.action)
        .all()
    )
    grok_veto = (
        db.query(func.count(AuditLog.id))
        .filter(AuditLog.created_at >= since, AuditLog.actor == "grok",
                AuditLog.action == "scanner_veto")
        .scalar() or 0
    )
    scan_runs = db.query(func.count(ScanRun.id)).filter(ScanRun.started_at >= since).scalar() or 0
    trade_count = (
        db.query(func.count(Candidate.id))
        .filter(Candidate.created_at >= since, Candidate.decision == "trade")
        .scalar() or 0
    )
    budget_skips, max_conc_skips = _scan_skipped_split(db, since)

    candidate_total = audit_counts.get("candidate", 0)
    bases = {"runs": scan_runs, "candidate": candidate_total}

    rows = []
    for stage in _STAGES:
        src = stage["source"]
        if src == "runs":
            count = scan_runs
        elif src == "budget_skip":
            count = budget_skips
        elif src == "max_concurrent_skip":
            count = max_conc_skips
        elif src == "trade":
            count = trade_count
        elif src == "scanner_veto":
            count = grok_veto
        else:
            count = audit_counts.get(src, 0)
        base = bases[stage["base"]]
        pct = round(count / base * 100, 1) if base else 0.0
        rows.append({
            "key": stage["key"], "label": stage["label"], "count": count,
            "pct": pct, "note": stage["note"](settings), "warn": False,
        })

    # The single most important fact this funnel exists to surface (§5.1.1): flag the
    # whole-cycle skip row(s) when a reason accounts for over half of all scan cycles.
    for r in rows:
        if r["key"] in ("scan_skipped_budget", "scan_skipped_max_concurrent") and scan_runs > 0:
            r["warn"] = r["count"] / scan_runs > 0.5

    return {
        "window": window,
        "since": since,
        "rows": rows,
        "scan_runs": scan_runs,
        "candidate_total": candidate_total,
        "trade_count": trade_count,
        "session_open": audit_counts.get("session_open", 0),
    }
