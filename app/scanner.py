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
from app.ta import bundle as ta_bundle

logger = logging.getLogger(__name__)

_MIN_CANDLES = 30


def _universe(db: Session, provider) -> list[str]:
    """Watchlist first, then ALL pairs above the liquidity floor, capped for safety.

    The exchange symbol list is cached in runtime_config so a transient provider hiccup
    (fetch_tickers failure → empty list) reuses the last good universe instead of silently
    collapsing the scan to just the 3-symbol watchlist. The degradation is audit-logged so it
    surfaces in the Nhật ký feed rather than looking like a config change.
    """
    symbols = list(settings.watchlist)
    fetched: list[str] = []
    try:
        fetched = [s for s in provider.all_symbols(settings.min_quote_volume) if s not in symbols]
    except Exception as exc:  # provider hiccup shouldn't kill the scan
        logger.warning("all_symbols failed: %s", exc)

    if fetched:
        runtime.set(db, "scanner_last_universe", json.dumps(fetched))
    else:
        # Provider returned nothing — reuse the last good list so a transient outage doesn't
        # shrink the scan to the watchlist alone.
        cached = runtime.get(db, "scanner_last_universe")
        if cached:
            try:
                fetched = [s for s in json.loads(cached) if s not in symbols]
            except (ValueError, TypeError):
                fetched = []
        audit.log(db, "scanner", "universe_degraded", entity="scan",
                  reused=len(fetched), source="cache" if fetched else "watchlist_only")

    return (symbols + fetched)[: settings.scan_max_symbols]


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

    universe = _universe(db, provider)
    scan = ScanRun(mode=mode, universe_size=len(universe), params=json.dumps(_thresholds()))
    db.add(scan)
    db.flush()
    audit.log(db, "scanner", "scan_start", entity=f"run:{scan.id}", mode=mode,
              universe=len(universe))

    backtest_agent = BacktestAgent()
    candidates: list[Candidate] = []
    # Candidates that passed every deterministic gate; opened after the (optional)
    # batched Grok review so the LLM is a single call/scan, not one call/symbol.
    to_open: list[dict] = []

    for symbol in universe:
        candles = provider.get_ohlcv(symbol, settings.backtest_timeframe,
                                     settings.backtest_lookback_days)
        if len(candles) < _MIN_CANDLES:
            continue

        distance_pct, tp_pct, max_waves = _effective_params(db, symbol)

        # Walk-forward: out-of-sample tail, with the live exits (stop-loss + fees) modelled
        # and overlapping entries decorrelated so the win-rate is realistic, not ~100%.
        wr = estimate_win_rate(
            candles, distance_pct, max_waves,
            tp_pct, settings.deadline_days, split=settings.walk_forward_split,
            sl_pct=settings.sl_pct, cost_pct=costengine.round_trip_cost_pct(),
            spacing_days=settings.backtest_trial_spacing_days,
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
            win_rate_lb=wr["win_rate_lb"], trials=wr["trials"], min_trials=settings.min_trials,
            expectancy=wr["expectancy"], min_expectancy=settings.min_expectancy_pct,
            max_loss_rate=settings.max_loss_rate, min_net_edge=settings.min_net_edge,
            **_thresholds(),
        )

        params_tag = f"d={distance_pct}/tp={tp_pct}/w={max_waves}"
        cand = Candidate(
            scan_id=scan.id, symbol=symbol, consensus_pct=consensus,
            win_rate=wr["win_rate"], win_rate_lb=wr["win_rate_lb"],
            expectancy=wr["expectancy"], trials=wr["trials"],
            est_days_to_tp=wr["avg_days_to_tp"],
            decision=d["decision"],
            reason="; ".join(d["reasons"])
                   + f" | win_lb={wr['win_rate_lb']:.0f}% E={wr['expectancy']:+.2f}%"
                   + f" n={wr['trials']} loss={wr['loss_rate']:.0f}% (stops={wr['stops']})"
                   + f" edge={net_edge:.2f}% | params {params_tag}",
        )
        db.add(cand)
        db.flush()
        audit.log(db, "scanner", "candidate", entity=symbol, decision=d["decision"],
                  consensus=consensus, win_rate=wr["win_rate"], win_rate_lb=wr["win_rate_lb"],
                  expectancy=wr["expectancy"], trials=wr["trials"], loss_rate=wr["loss_rate"],
                  net_edge=net_edge, days=wr["avg_days_to_tp"])

        if d["decision"] == "trade":
            blocked = _trade_block_reason(db, symbol)
            if blocked:
                cand.reason += f" | skipped: {blocked}"
            else:
                # Build the TA evidence bundle only for gate-bound candidates (the set the
                # Grok review actually decides on), and surface a compact tag on the reason.
                ta = ta_bundle.build(candles, db, symbol)
                cand.reason += f" | TA: {_ta_tag(ta)}"
                # Defer the actual open until after the batched Grok review.
                to_open.append({
                    "cand": cand, "symbol": symbol, "entry": candles[-1]["close"],
                    "distance_pct": distance_pct, "tp_pct": tp_pct, "max_waves": max_waves,
                    "consensus": consensus, "win_rate": wr["win_rate"],
                    "loss_rate": wr["loss_rate"], "net_edge": net_edge, "ta": ta,
                })
        candidates.append(cand)

    _review_and_open(db, to_open, mode)

    db.commit()
    return {"scan_id": scan.id, "mode": mode, "candidates": [c.to_dict() for c in candidates]}


def _ta_tag(ta: dict) -> str:
    """One-line TA summary for the scanner panel / audit (compact, human-readable)."""
    return (f"RSI{ta['rsi']:.0f} ADX{ta['adx']:.0f}/{ta['di']} MACDh{ta['macd_h']:+.2f} "
            f"%B{ta['bb_pct']:.2f} ATR{ta['atr_pct']:.1f}% ST:{ta['st']} HTF:{ta['htf']} "
            f"sup-{ta['sr_sup']:.1f}%/res+{ta['sr_res']:.1f}%")


def _trade_block_reason(db: Session, symbol: str) -> str | None:
    """Deterministic skip gates for a 'trade' candidate. Returns a reason string to skip,
    or None to proceed. Audits each block."""
    if _in_stop_cooldown(db, symbol):
        audit.log(db, "scanner", "skipped_cooldown", entity=symbol)
        return "stop-loss cooldown"
    block, streak = _loss_streak_block(db, symbol)
    if block:
        audit.log(db, "scanner", "skipped_loss_streak", entity=symbol, streak=streak,
                  window_days=settings.loss_streak_window_days)
        return f"thua {streak} lần liên tiếp trong {settings.loss_streak_window_days}d"
    if _symbol_at_cap(db, symbol):
        audit.log(db, "scanner", "skipped_concentration", entity=symbol)
        return "per-symbol session cap"
    if _owned_by_opus(db, symbol):
        audit.log(db, "scanner", "skipped_opus_owned", entity=symbol)
        return "OPUS đang giữ coin này"
    return None


def _review_and_open(db: Session, to_open: list[dict], mode: str) -> None:
    """Optional batched Grok endorse/veto pass, then open a KSS session per surviving
    candidate (still subject to the cumulative capital caps via _can_open). Grok is
    FAIL-OPEN: a symbol absent from the verdict map is treated as endorsed."""
    from app.orchestrator import grok  # lazy — avoid import-time coupling

    reviews: dict[str, dict] = {}
    if to_open and grok.scanner_enabled():
        items = [{
            "symbol": c["symbol"], "consensus": round(c["consensus"], 1),
            "win_rate": round(c["win_rate"], 1), "loss_rate": round(c["loss_rate"], 1),
            "net_edge": round(c["net_edge"], 2), "price": c["entry"],
            "ta": c.get("ta", {}),
        } for c in to_open]
        reviews = grok.review_candidates(db, items)

    for c in to_open:
        cand, symbol = c["cand"], c["symbol"]
        verdict = reviews.get(symbol)
        if verdict and not verdict["endorse"]:
            cand.reason += f" | Grok veto: {verdict['reason']}"
            audit.log(db, "grok", "scanner_veto", entity=symbol, reason=verdict["reason"])
            continue
        if verdict and verdict.get("reason"):
            cand.reason += f" | Grok: {verdict['reason']}"
        ok, why = _can_open(db)
        if ok:
            cand.session_id = _open_session(
                db, symbol, c["entry"], mode,
                distance_pct=c["distance_pct"], tp_pct=c["tp_pct"], max_waves=c["max_waves"],
            )
        else:
            cand.reason += f" | capped: {why}"
            audit.log(db, "scanner", "skipped_cap", entity=symbol, reason=why)


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


def _loss_streak_block(db: Session, symbol: str) -> tuple[bool, int]:
    """Block a pair on a recent consecutive-loss streak.

    Counts the symbol's most-recent run of losing closes (SELL fills, realized_pnl < 0)
    within the last `loss_streak_window_days`; a winning close breaks the run. Returns
    (block, streak). The block auto-decays: as losses age out of the window or a win
    lands, the streak falls below K and trading resumes — no manual blocklist to clear.
    """
    if not settings.loss_block_enabled or settings.loss_streak_block_k <= 0:
        return False, 0
    from datetime import datetime, timedelta

    from app.models import Fill

    cutoff = datetime.utcnow() - timedelta(days=settings.loss_streak_window_days)
    closes = (
        db.query(Fill)
        .filter(
            Fill.symbol == symbol,
            Fill.side == "SELL",
            Fill.realized_pnl != 0,
            Fill.executed_at >= cutoff,
        )
        .order_by(Fill.executed_at.desc())
        .all()
    )
    streak = 0
    for f in closes:
        if (f.realized_pnl or 0.0) < 0:
            streak += 1
        else:
            break  # a winning close breaks the most-recent streak
    return streak >= settings.loss_streak_block_k, streak


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


def _owned_by_opus(db: Session, symbol: str) -> bool:
    """True if OPUS currently manages `symbol` (watch/ride) — K-1 strategy exclusivity, so
    KSS and OPUS never blend cost bases on the same coin. (Rescue is a handoff, not ownership.)"""
    from app.orchestrator.models import OPUS_RIDE, OPUS_WATCH, OpusPosition

    return (
        db.query(OpusPosition)
        .filter(OpusPosition.symbol == symbol, OpusPosition.state.in_((OPUS_WATCH, OPUS_RIDE)))
        .count()
        > 0
    )


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
