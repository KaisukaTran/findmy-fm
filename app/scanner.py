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

from app import audit, costengine, hyperopt, ml, orders, runtime
from app.agents import SIGNAL_AGENTS, BacktestAgent, aggregate, decide
from app.backtest import estimate_win_rate
from app.config import settings
from app.data.providers import data_provider
from app.kss import service
from app.models import SESSION_ACTIVE, AgentVoteRecord, Candidate, KssSession, PendingOrder, ScanRun

logger = logging.getLogger(__name__)

_MIN_CANDLES = 30


def _universe(provider) -> list[str]:
    """Watchlist first, then ALL pairs above the liquidity floor, capped for safety."""
    symbols = list(settings.watchlist)
    try:
        for s in provider.all_symbols(settings.min_quote_volume):
            if s not in symbols:
                symbols.append(s)
    except Exception as exc:  # provider hiccup shouldn't kill the watchlist scan
        logger.warning("all_symbols failed: %s", exc)
    return symbols[: settings.scan_max_symbols]


def _thresholds() -> dict:
    return {
        "min_win_rate": settings.min_win_rate,
        "min_confidence": settings.min_confidence,
        "deadline_days": settings.deadline_days,
    }


def _effective_params(db: Session, symbol: str) -> tuple[float, float, int]:
    """Return (distance_pct, tp_pct, max_waves) for a symbol.

    Uses hyperopt-tuned values when hyperopt_enabled and a row exists;
    falls back to global scan_* defaults in all other cases.
    """
    if settings.hyperopt_enabled:
        row = hyperopt.best_params(db, symbol)
        if row is not None:
            return row.distance_pct, row.tp_pct, row.max_waves
    return settings.scan_distance_pct, settings.scan_tp_pct, settings.scan_max_waves


def run_scan(db: Session, mode: str | None = None) -> dict:
    """Run one full scan; returns {scan_id, mode, candidates:[...]}."""
    provider = data_provider()
    mode = mode or ("auto" if settings.auto_trade else "semi")

    service.sweep_deadlines(db)  # housekeeping: close anything past its deadline

    # Load ML model once for the whole scan; None when ml disabled.
    ml_model = ml.load_latest(db) if settings.ml_enabled else None

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

        distance_pct, tp_pct, max_waves = _effective_params(db, symbol)

        # Walk-forward: metric on the out-of-sample tail (regime-current, less overfit).
        wr = estimate_win_rate(
            candles, distance_pct, max_waves,
            tp_pct, settings.deadline_days, split=settings.walk_forward_split,
        )
        ctx = {
            "win_rate": wr["win_rate"], "trials": wr["trials"],
            "avg_days_to_tp": wr["avg_days_to_tp"],
            "ml_model": ml_model,
        }

        votes = [a.evaluate(symbol, candles, ctx) for a in SIGNAL_AGENTS]
        votes.append(backtest_agent.evaluate(symbol, candles, ctx))
        for v in votes:
            db.add(AgentVoteRecord(scan_id=scan.id, symbol=symbol, agent_name=v.name,
                                   score=v.score, confidence=v.confidence, reason=v.reason))

        consensus = aggregate(votes)
        net_edge = costengine.net_edge_pct(tp_pct)
        d = decide(
            consensus, wr["win_rate"], wr["avg_days_to_tp"],
            loss_rate=wr["loss_rate"], net_edge=net_edge,
            max_loss_rate=settings.max_loss_rate, min_net_edge=settings.min_net_edge,
            **_thresholds(),
        )

        params_tag = f"d={distance_pct}/tp={tp_pct}/w={max_waves}"
        cand = Candidate(
            scan_id=scan.id, symbol=symbol, consensus_pct=consensus,
            win_rate=wr["win_rate"], est_days_to_tp=wr["avg_days_to_tp"],
            decision=d["decision"],
            reason="; ".join(d["reasons"])
                   + f" | loss={wr['loss_rate']:.0f}% edge={net_edge:.2f}% | params {params_tag}",
        )
        db.add(cand)
        db.flush()
        audit.log(db, "scanner", "candidate", entity=symbol, decision=d["decision"],
                  consensus=consensus, win_rate=wr["win_rate"], loss_rate=wr["loss_rate"],
                  net_edge=net_edge, days=wr["avg_days_to_tp"])

        if d["decision"] == "trade":
            if _in_stop_cooldown(db, symbol):
                cand.reason += " | skipped: stop-loss cooldown"
                audit.log(db, "scanner", "skipped_cooldown", entity=symbol)
            elif _symbol_at_cap(db, symbol):
                cand.reason += " | skipped: per-symbol session cap"
                audit.log(db, "scanner", "skipped_concentration", entity=symbol)
            else:
                ok, why = _can_open(db)
                if ok:
                    cand.session_id = _open_session(
                        db, symbol, candles[-1]["close"], mode,
                        distance_pct=distance_pct, tp_pct=tp_pct, max_waves=max_waves,
                    )
                else:
                    cand.reason += f" | capped: {why}"
                    audit.log(db, "scanner", "skipped_cap", entity=symbol, reason=why)
        candidates.append(cand)

    db.commit()
    return {"scan_id": scan.id, "mode": mode, "candidates": [c.to_dict() for c in candidates]}


def _can_open(db: Session) -> tuple[bool, str]:
    """Capital-preservation caps: concurrent sessions, deployed capital, min notional."""
    active = db.query(KssSession).filter(KssSession.status == SESSION_ACTIVE).all()
    if len(active) >= settings.max_concurrent_sessions:
        return False, f"max concurrent {settings.max_concurrent_sessions}"
    deployed = sum(s.isolated_fund for s in active)
    cap = settings.account_equity * settings.max_deployed_pct / 100
    if deployed + settings.scan_fund > cap:
        return False, f"deployed cap {settings.max_deployed_pct:.0f}% of equity"
    if not costengine.notional_ok(settings.scan_fund):
        return False, "below min notional"
    return True, ""


def _in_stop_cooldown(db: Session, symbol: str) -> bool:
    """True if `symbol` was stopped-out within the last `stop_cooldown_min` minutes."""
    if settings.stop_cooldown_min <= 0:
        return False
    ts = runtime.get(db, f"stop_cooldown:{symbol}")
    if not ts:
        return False
    from datetime import datetime

    try:
        stopped_at = datetime.fromisoformat(ts)
    except ValueError:
        return False
    elapsed_min = (datetime.utcnow() - stopped_at).total_seconds() / 60.0
    return elapsed_min < settings.stop_cooldown_min


def _symbol_at_cap(db: Session, symbol: str) -> bool:
    """True if `symbol` already has the max concurrent ACTIVE sessions (concentration cap)."""
    if settings.max_sessions_per_symbol <= 0:
        return False
    n = (
        db.query(KssSession)
        .filter(KssSession.symbol == symbol, KssSession.status == SESSION_ACTIVE)
        .count()
    )
    return n >= settings.max_sessions_per_symbol


def _open_session(
    db: Session,
    symbol: str,
    entry: float,
    mode: str,
    *,
    distance_pct: float = settings.scan_distance_pct,
    tp_pct: float = settings.scan_tp_pct,
    max_waves: int = settings.scan_max_waves,
) -> int:
    """Open a KSS session using effective (possibly hyperopt-tuned) params."""
    row = service.create_session(
        db,
        symbol=symbol,
        entry_price=entry,
        distance_pct=distance_pct,
        max_waves=max_waves,
        isolated_fund=settings.scan_fund,
        tp_pct=tp_pct,
        # Deadline (not the intra-fill timeout) governs the hold; keep timeout long.
        timeout_x_min=float(settings.deadline_days * 1440),
        gap_y_min=0.0,
        deadline_days=settings.deadline_days,
        note=f"auto-scanner ({mode})",
    )
    started = service.start_session(db, row.id)
    audit.log(db, "scanner", "session_open", entity=f"kss:{row.id}", symbol=symbol, mode=mode)

    if mode == "auto":
        if not runtime.is_frozen(db):
            oid = started["pending_order_id"]
            _vetoed = False
            from app import guardian  # lazy — avoid import-time cost
            if guardian.enabled():
                pending_order = db.get(PendingOrder, oid)
                if pending_order is not None:
                    vetoes = guardian.review([pending_order])
                    if oid in vetoes:
                        pending_order.auto_veto = True
                        pending_order.auto_veto_reason = vetoes[oid]
                        audit.log(db, "scanner", "guardian_veto", entity=f"order:{oid}",
                                  symbol=symbol, session=row.id)
                        _vetoed = True
            if not _vetoed:
                orders.approve_order(db, oid, reviewer="auto-trader")
                audit.log(db, "auto-trader", "auto_approve", entity=f"order:{oid}",
                          symbol=symbol, session=row.id)
        else:
            audit.log(db, "scanner", "auto_skip_frozen",
                      entity=f"order:{started['pending_order_id']}", symbol=symbol, session=row.id)
    return row.id
