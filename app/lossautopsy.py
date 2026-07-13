"""
Loss autopsy: session-level join of realized KSS exits back to the scanner's
entry-time candidate metrics, root-cause tagging, and a win/loss discrimination
report. Read-only — never touches strategy/scanner logic.

This is the DEEP layer that `app.portfolio.loss_analysis` (per-fill cause tags)
lacks: it groups exits by KSS *session*, joins the entry-time `Candidate` row
that opened it, and asks "did the entry signal predict this outcome?" Reuses
`portfolio._loss_tag` for the exit-kind label rather than duplicating it.
"""

from __future__ import annotations

import math
import re
import statistics

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import portfolio, timefmt
from app.models import Candidate, Fill, KssSession, KssWave

_SESSION_REF_RE = re.compile(r"^pyramid:(\d+):")

# Case-dict keys used for the win/loss discrimination split.
_DISCRIM_METRICS = ("consensus", "win_rate_lb", "expectancy", "worst_mae", "avg_mae")


def _quantiles(xs: list[float]) -> dict | None:
    """Nearest-rank p25/median/p75 for an already ascending-sorted list.

    Empty input -> None. Pure helper, no DB/IO.
    """
    if not xs:
        return None
    n = len(xs)
    return {
        "n": n,
        "med": statistics.median(xs),
        "p25": xs[n // 4],
        "p75": xs[3 * n // 4],
    }


def _percentile(sorted_xs: list[float], p: float) -> float:
    """Nearest-rank percentile (p in [0,1]) of an ascending-sorted list."""
    n = len(sorted_xs)
    if n == 0:
        return 0.0
    rank = max(1, math.ceil(p * n))
    return sorted_xs[min(rank, n) - 1]


def _has_dup_wave(db: Session, sid: int) -> bool:
    """True if any wave_num repeats for this session (the C −$12.6k structural bug)."""
    row = (
        db.query(KssWave.wave_num)
        .filter(KssWave.session_id == sid)
        .group_by(KssWave.wave_num)
        .having(func.count(KssWave.id) > 1)
        .first()
    )
    return row is not None


def _root_cause(case: dict, p95_deploy: float) -> str:
    """First-match root-cause tag for a LOSING case. Callers only invoke this for losers."""
    if case["sid"] is None or case["_session_missing"]:
        return "orphan"
    if case["_dup_wave"]:
        return "dup_wave"
    if case["deploy_usd"] is not None and case["deploy_usd"] >= p95_deploy:
        return "size_outlier"
    if (
        case["mode"] == "pyramid_up"
        and case["exit_kind"] == "KSS-SL"
        and (case["waves_reached"] or 0) <= 1
    ):
        return "pyramid_up_reversal"
    if (
        case["mode"] == "dca_down"
        and case["exit_kind"] == "KSS-SL"
        and case["max_waves"]
        and case["waves_reached"] is not None
        and case["waves_reached"] >= math.ceil(case["max_waves"] * 0.5)
    ):
        return "deep_ladder_sl"
    return "reversal"


def autopsy(db: Session, limit: int = 2000) -> dict:
    """Session-level loss autopsy for KSS pyramid sessions. Read-only.

    Joins each session that has realized SELL exits to its entry-time Candidate
    metrics, labels win/loss, tags a root cause, and builds a win/loss
    discrimination report.
    """
    fills = (
        db.query(Fill)
        .filter(Fill.side == "SELL", Fill.realized_pnl.isnot(None))
        .order_by(Fill.executed_at.desc())
        .limit(limit)
        .all()
    )
    fills.reverse()  # ascending chronological order (most-recent `limit` kept)

    buckets: dict[int | None, list[Fill]] = {}
    for f in fills:
        ref = f.source_ref or ""
        if ref.startswith("opus:"):
            continue  # different subsystem — out of scope
        m = _SESSION_REF_RE.match(ref)
        if m:
            sid: int | None = int(m.group(1))
        elif ref.startswith("orphan:"):
            sid = None
        else:
            continue  # unparseable — skip
        buckets.setdefault(sid, []).append(f)

    # Pass 1: build every case (root_cause left blank; needs the p95 across all cases).
    cases: list[dict] = []
    for sid, bucket_fills in buckets.items():
        net = sum(float(fill.realized_pnl or 0.0) for fill in bucket_fills)
        label = "loss" if net < 0 else "win"
        last_fill = bucket_fills[-1]
        exit_kind = portfolio._loss_tag(last_fill.source_ref)
        exit_at = last_fill.executed_at

        session_row = db.get(KssSession, sid) if sid is not None else None
        session_missing = sid is not None and session_row is None

        if session_row is not None:
            symbol = session_row.symbol
            mode = session_row.strategy_mode
            status = session_row.status
            deploy_usd: float | None = session_row.total_cost
            waves_reached: int | None = session_row.current_wave
            max_waves: int | None = session_row.max_waves
            entry_at = session_row.started_at
            dup_wave = _has_dup_wave(db, sid)
        else:
            symbol = last_fill.symbol
            mode = "?"
            status = "?"
            deploy_usd = None
            waves_reached = None
            max_waves = None
            entry_at = None
            dup_wave = False

        candidate = (
            db.query(Candidate)
            .filter(Candidate.session_id == sid)
            .order_by(Candidate.id.desc())
            .first()
            if sid is not None
            else None
        )
        if candidate is not None:
            consensus = candidate.consensus_pct
            win_rate_lb = candidate.win_rate_lb
            expectancy = candidate.expectancy
            trials = candidate.trials
            avg_mae = candidate.avg_mae
            worst_mae = candidate.worst_mae
            grok_endorsed = "grok" in (candidate.reason or "").lower()
        else:
            consensus = win_rate_lb = expectancy = avg_mae = worst_mae = None
            trials = None
            grok_endorsed = False

        hold_min = None
        if entry_at is not None and exit_at is not None:
            hold_min = (exit_at - entry_at).total_seconds() / 60.0

        cases.append(
            {
                "sid": sid,
                "symbol": symbol,
                "mode": mode,
                "status": status,
                "net": net,
                "label": label,
                "exit_kind": exit_kind,
                "deploy_usd": deploy_usd,
                "waves_reached": waves_reached,
                "max_waves": max_waves,
                "hold_min": hold_min,
                "consensus": consensus,
                "win_rate_lb": win_rate_lb,
                "expectancy": expectancy,
                "trials": trials,
                "avg_mae": avg_mae,
                "worst_mae": worst_mae,
                "grok_endorsed": grok_endorsed,
                "root_cause": "",
                "entry_at": timefmt.local_dt(entry_at) if entry_at else "",
                "exit_at": timefmt.local_dt(exit_at) if exit_at else "",
                # internal scratch fields, stripped before return
                "_session_missing": session_missing,
                "_dup_wave": dup_wave,
            }
        )

    deploy_values = sorted(c["deploy_usd"] for c in cases if c["deploy_usd"] is not None)
    p95_deploy = _percentile(deploy_values, 0.95)

    # Pass 2: assign root causes (losers only) now that p95_deploy is known.
    for case in cases:
        if case["label"] == "loss":
            case["root_cause"] = _root_cause(case, p95_deploy)
        del case["_session_missing"]
        del case["_dup_wave"]

    cases.sort(key=lambda c: c["net"])  # biggest loss first

    winners = [c for c in cases if c["label"] == "win"]
    losers = [c for c in cases if c["label"] == "loss"]
    win_total = sum(c["net"] for c in winners)
    loss_total = sum(c["net"] for c in losers)
    summary = {
        "sessions": len(cases),
        "winners": len(winners),
        "losers": len(losers),
        "net_realized": win_total + loss_total,
        "loss_total": loss_total,
        "win_total": win_total,
    }

    by_root_cause: dict[str, dict] = {}
    for c in losers:
        tag = c["root_cause"] or "reversal"
        entry = by_root_cause.setdefault(tag, {"count": 0, "total": 0.0})
        entry["count"] += 1
        entry["total"] += c["net"]
    by_root_cause = dict(sorted(by_root_cause.items(), key=lambda kv: kv[1]["total"]))

    discrimination: dict[str, dict] = {}
    for metric in _DISCRIM_METRICS:
        win_vals = sorted(c[metric] for c in winners if c[metric] is not None)
        loss_vals = sorted(c[metric] for c in losers if c[metric] is not None)
        discrimination[metric] = {"win": _quantiles(win_vals), "loss": _quantiles(loss_vals)}

    return {
        "cases": cases,
        "summary": summary,
        "by_root_cause": by_root_cause,
        "discrimination": discrimination,
    }
