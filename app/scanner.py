"""
Multi-agent scanner — the decision layer that ties everything together.

For each symbol in the universe (watchlist ∪ top-N by volume):
  1. fetch real historical candles from the (no-key) data provider
  2. estimate the KSS win-rate via backtest
  3. collect votes from the quant agents (+ the backtest agent)
  4. aggregate into a consensus % and decide trade/skip against the thresholds
  5. persist ScanRun / AgentVoteRecord / Candidate + AuditLog (full audit trail)
  6. for a "trade" decision, open a KSS session:
       - semi-auto: wave 0 lands in the pending-approval queue (human approves)
       - full-auto (settings.auto_trade): wave 0 is auto-approved (still risk-checked)

Nothing bypasses the approval queue; full-auto just adds an audited auto-approval.
The ≥80% win-rate and ≤30-day deadline gates live in app.agents.aggregator.decide
and app.config.settings.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app import audit, orders
from app.agents import SIGNAL_AGENTS, BacktestAgent, aggregate, decide
from app.backtest import estimate_win_rate
from app.config import settings
from app.data.providers import data_provider
from app.kss import service
from app.models import AgentVoteRecord, Candidate, ScanRun

logger = logging.getLogger(__name__)

_MIN_CANDLES = 30


def _universe(provider) -> list[str]:
    """Watchlist first, then any top-N-by-volume symbols not already present."""
    symbols = list(settings.watchlist)
    try:
        for s in provider.top_symbols(settings.scan_top_n):
            if s not in symbols:
                symbols.append(s)
    except Exception as exc:  # provider hiccup shouldn't kill the watchlist scan
        logger.warning("top_symbols failed: %s", exc)
    return symbols


def _thresholds() -> dict:
    return {
        "min_win_rate": settings.min_win_rate,
        "min_confidence": settings.min_confidence,
        "deadline_days": settings.deadline_days,
    }


def run_scan(db: Session, mode: str | None = None) -> dict:
    """Run one full scan; returns {scan_id, mode, candidates:[...]}."""
    provider = data_provider()
    mode = mode or ("auto" if settings.auto_trade else "semi")

    service.sweep_deadlines(db)  # housekeeping: close anything past its deadline

    universe = _universe(provider)
    scan = ScanRun(mode=mode, universe_size=len(universe), params=json.dumps(_thresholds()))
    db.add(scan)
    db.flush()
    audit.log(db, "scanner", "scan_start", entity=f"run:{scan.id}", mode=mode,
              universe=len(universe))

    backtest_agent = BacktestAgent()
    candidates: list[Candidate] = []

    for symbol in universe:
        candles = provider.get_ohlcv(symbol, settings.backtest_timeframe,
                                     settings.backtest_lookback_days)
        if len(candles) < _MIN_CANDLES:
            continue

        wr = estimate_win_rate(
            candles, settings.scan_distance_pct, settings.scan_max_waves,
            settings.scan_tp_pct, settings.deadline_days,
        )
        ctx = {"win_rate": wr["win_rate"], "trials": wr["trials"],
               "avg_days_to_tp": wr["avg_days_to_tp"]}

        votes = [a.evaluate(symbol, candles, ctx) for a in SIGNAL_AGENTS]
        votes.append(backtest_agent.evaluate(symbol, candles, ctx))
        for v in votes:
            db.add(AgentVoteRecord(scan_id=scan.id, symbol=symbol, agent_name=v.name,
                                   score=v.score, confidence=v.confidence, reason=v.reason))

        consensus = aggregate(votes)
        d = decide(consensus, wr["win_rate"], wr["avg_days_to_tp"], **_thresholds())

        cand = Candidate(
            scan_id=scan.id, symbol=symbol, consensus_pct=consensus,
            win_rate=wr["win_rate"], est_days_to_tp=wr["avg_days_to_tp"],
            decision=d["decision"], reason="; ".join(d["reasons"]),
        )
        db.add(cand)
        db.flush()
        audit.log(db, "scanner", "candidate", entity=symbol, decision=d["decision"],
                  consensus=consensus, win_rate=wr["win_rate"], days=wr["avg_days_to_tp"])

        if d["decision"] == "trade":
            cand.session_id = _open_session(db, symbol, candles[-1]["close"], mode)
        candidates.append(cand)

    db.commit()
    return {"scan_id": scan.id, "mode": mode, "candidates": [c.to_dict() for c in candidates]}


def _open_session(db: Session, symbol: str, entry: float, mode: str) -> int:
    """Open a KSS session from scan defaults; auto-approve wave 0 in full-auto."""
    row = service.create_session(
        db,
        symbol=symbol,
        entry_price=entry,
        distance_pct=settings.scan_distance_pct,
        max_waves=settings.scan_max_waves,
        isolated_fund=settings.scan_fund,
        tp_pct=settings.scan_tp_pct,
        # Deadline (not the intra-fill timeout) governs the hold; keep timeout long.
        timeout_x_min=float(settings.deadline_days * 1440),
        gap_y_min=0.0,
        deadline_days=settings.deadline_days,
        note=f"auto-scanner ({mode})",
    )
    started = service.start_session(db, row.id)
    audit.log(db, "scanner", "session_open", entity=f"kss:{row.id}", symbol=symbol, mode=mode)

    if mode == "auto":
        orders.approve_order(db, started["pending_order_id"], reviewer="auto-trader")
        audit.log(db, "auto-trader", "auto_approve", entity=f"order:{started['pending_order_id']}",
                  symbol=symbol, session=row.id)
    return row.id
