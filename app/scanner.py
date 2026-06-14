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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import audit, costengine, hyperopt, ml, orders, runtime
from app.agents import SIGNAL_AGENTS, BacktestAgent, aggregate, decide
from app.backtest import estimate_win_rate
from app.config import settings
from app.data import candle_cache
from app.data.providers import CcxtProvider, data_provider
from app.kss import service
from app.models import SESSION_ACTIVE, AgentVoteRecord, Candidate, KssSession, PendingOrder, ScanRun
from app.ta import bundle as ta_bundle

logger = logging.getLogger(__name__)

_MIN_CANDLES = 30

_PARALLEL_WORKERS = 4  # S2: parallel OHLCV fetch workers

# S5: cap the Grok review batch so one fat scan cannot blow the token budget.
# Candidates are sorted by expectancy descending; only the top N enter the LLM call.
# Under fail_mode="closed" the dropped symbols (beyond the cap) also do not open this
# scan because they have no explicit endorse verdict — same semantics as a Grok outage.
# Under fail_mode="open" (default) the dropped symbols open exactly as today.
_GROK_REVIEW_BATCH_MAX = 8


def _provider_factory(exchange_id: str) -> CcxtProvider:
    """Return a *fresh* CcxtProvider for the given exchange.

    Called inside each ThreadPoolExecutor worker so every thread owns its own
    ccxt client.  ccxt sync instances share HTTP session state and are NOT
    thread-safe across concurrent callers; one-client-per-worker avoids locks
    and delivers true parallelism.  (enableRateLimit=True is set by default on
    each fresh instance so per-exchange rate limits are still respected.)
    """
    return CcxtProvider(exchange_id)


def _prefetch_candles(
    exchange_id: str,
    symbols: list[str],
    timeframe: str,
    limit: int,
) -> dict[str, tuple[list, bool]]:
    """Warm the candle cache for *symbols* in parallel.

    Returns a dict ``symbol -> (candles, was_hit)`` for every symbol in the
    input list.  Cache-hit symbols are resolved immediately without spawning a
    thread; only cache-miss symbols are fetched in the thread pool (up to
    ``_PARALLEL_WORKERS`` concurrent threads).

    Any individual symbol failure returns ``([], False)`` — the caller treats
    that as thin data and audits via *skipped_thin_data* (unchanged behaviour).
    """
    results: dict[str, tuple[list, bool]] = {}
    misses: list[str] = []

    # First pass: collect hits without touching the network.
    for sym in symbols:
        key = (exchange_id, sym, timeframe)
        entry = candle_cache._cache.get(key)
        if entry is not None and entry.is_fresh(timeframe):
            results[sym] = (entry.candles, True)
        else:
            misses.append(sym)

    if not misses:
        return results

    # Second pass: fetch misses in parallel.
    def _fetch(sym: str) -> tuple[str, list, bool]:
        candles, hit = candle_cache.get_candles(
            exchange_id, sym, timeframe, limit, _provider_factory
        )
        return sym, candles, hit

    with ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
        futures = {pool.submit(_fetch, sym): sym for sym in misses}
        for future in as_completed(futures):
            sym, candles, hit = future.result()
            results[sym] = (candles, hit)

    return results

# Bars per day for each supported timeframe — used to convert the lookback_days setting
# into the correct bar ``limit`` regardless of the active timeframe.
_BARS_PER_DAY: dict[str, float] = {
    "1m": 1440.0, "3m": 480.0, "5m": 288.0, "15m": 96.0, "30m": 48.0,
    "1h": 24.0, "2h": 12.0, "4h": 6.0, "6h": 4.0, "8h": 3.0, "12h": 2.0,
    "1d": 1.0, "3d": 1 / 3, "1w": 1 / 7,
}


def _days_to_bars(days: int, timeframe: str) -> int:
    """Convert a lookback in calendar days to the nearest whole number of bars.

    Falls back to 1 bar/day for unknown timeframes, which is safe (returns *days*
    unchanged) and avoids silently under-fetching on new timeframe strings.
    """
    bars_per_day = _BARS_PER_DAY.get(timeframe, 1.0)
    return max(_MIN_CANDLES, round(days * bars_per_day))


_UNIVERSE_TTL_HOURS = 24  # B7: how long the cached exchange symbol list stays fresh


def _universe(db: Session, provider) -> list[str]:
    """Watchlist first, then ALL pairs above the liquidity floor, capped for safety.

    The exchange symbol list is cached in runtime_config with a timestamp so a transient
    provider hiccup reuses the last *fresh* universe instead of silently collapsing the scan
    to the watchlist alone.  The cache is only written when the symbol list actually changes
    (avoids per-scan DB churn) and expires after ``_UNIVERSE_TTL_HOURS`` hours so it never
    drifts stale forever.  Degradation is audit-logged so it surfaces in the Nhật ký feed.
    """
    symbols = list(settings.watchlist)
    fetched: list[str] = []
    try:
        fetched = [s for s in provider.all_symbols(settings.min_quote_volume) if s not in symbols]
    except Exception as exc:  # provider hiccup shouldn't kill the scan
        logger.warning("all_symbols failed: %s", exc)

    if fetched:
        # Write only when the symbol list has changed — avoids per-scan DB churn (B7).
        cached_raw = runtime.get(db, "scanner_last_universe")
        existing: list[str] = []
        if cached_raw:
            try:
                payload = json.loads(cached_raw)
                existing = payload.get("symbols", []) if isinstance(payload, dict) else payload
            except (ValueError, TypeError):
                pass
        if existing != fetched:
            runtime.set(db, "scanner_last_universe",
                        json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "symbols": fetched}))
    else:
        # Provider returned nothing — try the cache but honour the TTL (B7).
        cached_raw = runtime.get(db, "scanner_last_universe")
        if cached_raw:
            try:
                payload = json.loads(cached_raw)
                if isinstance(payload, dict):
                    cached_syms = payload.get("symbols", [])
                    cached_ts_str = payload.get("ts")
                    if cached_ts_str:
                        cached_ts = datetime.fromisoformat(cached_ts_str)
                        # Make aware if stored without tzinfo (legacy rows).
                        if cached_ts.tzinfo is None:
                            cached_ts = cached_ts.replace(tzinfo=timezone.utc)
                        age_h = (datetime.now(timezone.utc) - cached_ts).total_seconds() / 3600
                        if age_h <= _UNIVERSE_TTL_HOURS:
                            fetched = [s for s in cached_syms if s not in symbols]
                        else:
                            logger.warning("universe cache expired (%.0fh old) — watchlist only", age_h)
                    else:
                        fetched = [s for s in cached_syms if s not in symbols]
                else:
                    # Legacy plain-list value — migrate transparently.
                    fetched = [s for s in payload if s not in symbols]
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

    Design: hyperopt tunes WATCHLIST symbols only; this function falls back to global
    scan_* params for non-watchlist (universe) symbols by design. This keeps tuning
    focused and allows the global defaults to govern exploration.
    """
    if settings.hyperopt_enabled:
        row = hyperopt.best_params(db, symbol)
        if row is not None:
            return row.distance_pct, row.tp_pct, row.max_waves
    return settings.scan_distance_pct, settings.scan_tp_pct, settings.scan_max_waves


def run_scan(db: Session, mode: str | None = None) -> dict:
    """Run one full scan; returns {scan_id, mode, candidates:[...]}."""
    _scan_start_mono = time.monotonic()  # S2: wall-clock for scan_duration_ms
    provider = data_provider()
    mode = mode or ("auto" if settings.auto_trade else "semi")

    service.sweep_deadlines(db)  # housekeeping: close anything past its deadline

    # Load ML model once for the whole scan; None when ml disabled.
    ml_model = ml.load_latest(db) if settings.ml_enabled else None

    # S6: per-stage monotonic checkpoints — accumulated ms totals, cheap.
    _t0 = time.monotonic()
    universe = _universe(db, provider)
    t_universe_ms = int((time.monotonic() - _t0) * 1000)

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
    _skipped_thin: list[str] = []  # B6: symbols skipped for insufficient candles

    # S3: cheap-gates-first — run the four deterministic block checks (cooldown /
    # loss-streak / per-symbol cap / OPUS-owned) for every universe symbol BEFORE
    # touching the candle cache.  Blocked symbols get a skip Candidate immediately
    # and are removed from the fetch set so they trigger ZERO OHLCV calls.
    _n_pre_blocked = 0
    to_fetch: list[str] = []
    for symbol in universe:
        block_reason = _trade_block_reason(db, symbol)
        if block_reason:
            _n_pre_blocked += 1
            cand = Candidate(
                scan_id=scan.id, symbol=symbol,
                consensus_pct=0.0, win_rate=0.0, win_rate_lb=0.0,
                expectancy=0.0, trials=0, est_days_to_tp=None,
                decision="skip",
                reason=f"pre-blocked: {block_reason}",
            )
            db.add(cand)
            db.flush()
            audit.log(db, "scanner", "candidate", entity=symbol, decision="skip",
                      consensus=0.0, win_rate=0.0, win_rate_lb=0.0,
                      expectancy=0.0, trials=0, loss_rate=0.0,
                      net_edge=0.0, days=None, pre_blocked=block_reason)
            candidates.append(cand)
        else:
            to_fetch.append(symbol)

    # S2: warm the candle cache for unblocked symbols only (blocked symbols have
    # already been recorded above and must not trigger any OHLCV network calls).
    limit = _days_to_bars(settings.backtest_lookback_days, settings.backtest_timeframe)
    exchange_id = settings.data_exchange
    _t_fetch = time.monotonic()
    _candle_map = _prefetch_candles(
        exchange_id, to_fetch, settings.backtest_timeframe, limit
    )
    t_fetch_ms = int((time.monotonic() - _t_fetch) * 1000)
    _cache_hits = sum(1 for _, hit in _candle_map.values() if hit)
    _cache_misses = len(to_fetch) - _cache_hits

    # S6: accumulate per-symbol backtest + votes time across the loop.
    t_backtest_ms = 0
    t_votes_ms = 0
    _n_skipped_thin = 0

    for symbol in to_fetch:
        candles, _hit = _candle_map.get(symbol, ([], False))
        if len(candles) < _MIN_CANDLES:
            _skipped_thin.append(symbol)
            _n_skipped_thin += 1
            continue

        distance_pct, tp_pct, max_waves = _effective_params(db, symbol)

        # Walk-forward: out-of-sample tail, with the live exits (stop-loss + fees) modelled
        # and overlapping entries decorrelated so the win-rate is realistic, not ~100%.
        _tb = time.monotonic()
        wr = estimate_win_rate(
            candles, distance_pct, max_waves,
            tp_pct, settings.deadline_days, split=settings.walk_forward_split,
            sl_pct=settings.sl_pct, cost_pct=costengine.round_trip_cost_pct(),
            spacing_days=settings.backtest_trial_spacing_days,
        )
        t_backtest_ms += int((time.monotonic() - _tb) * 1000)

        ctx = {
            "win_rate": wr["win_rate"], "win_rate_lb": wr["win_rate_lb"],
            "trials": wr["trials"],
            "avg_days_to_tp": wr["avg_days_to_tp"],
            "ml_model": ml_model,
        }

        _tv = time.monotonic()
        votes = [a.evaluate(symbol, candles, ctx) for a in SIGNAL_AGENTS]
        votes.append(backtest_agent.evaluate(symbol, candles, ctx))
        t_votes_ms += int((time.monotonic() - _tv) * 1000)

        for v in votes:
            db.add(AgentVoteRecord(scan_id=scan.id, symbol=symbol, agent_name=v.name,
                                   score=v.score, confidence=v.confidence, reason=v.reason))

        # S4: weights resolved at call time from runtime_config so dashboard edits
        # are picked up without a restart; backtest weight is 0 per S4 contract.
        consensus = aggregate(votes, weights=runtime.get_consensus_weights(db))
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
                   + f" n={wr['trials']} win={wr['win_rate']:.0f}% loss={wr['loss_rate']:.0f}%"
                   + f" flat={wr['flat_rate']:.0f}% (stops={wr['stops']})"
                   + f" edge={net_edge:.2f}% | params {params_tag}",
        )
        db.add(cand)
        db.flush()
        audit.log(db, "scanner", "candidate", entity=symbol, decision=d["decision"],
                  consensus=consensus, win_rate=wr["win_rate"], win_rate_lb=wr["win_rate_lb"],
                  expectancy=wr["expectancy"], trials=wr["trials"], loss_rate=wr["loss_rate"],
                  net_edge=net_edge, days=wr["avg_days_to_tp"])

        if d["decision"] == "trade":
            # Build the TA evidence bundle only for gate-bound candidates (the set the
            # Grok review actually decides on), and surface a compact tag on the reason.
            ta = ta_bundle.build(candles, db, symbol)
            cand.reason = (cand.reason or "") + f" | TA: {_ta_tag(ta)}"
            # Defer the actual open until after the batched Grok review.
            to_open.append({
                "cand": cand, "symbol": symbol, "entry": candles[-1]["close"],
                "distance_pct": distance_pct, "tp_pct": tp_pct, "max_waves": max_waves,
                "consensus": consensus, "win_rate": wr["win_rate"],
                "loss_rate": wr["loss_rate"], "net_edge": net_edge, "ta": ta,
            })
        candidates.append(cand)

    # B6: one compact audit entry per scan listing all thin/failed symbols together.
    if _skipped_thin:
        audit.log(db, "scanner", "skipped_thin_data", entity=f"run:{scan.id}",
                  count=len(_skipped_thin), symbols=",".join(_skipped_thin[:20]))

    _t_grok = time.monotonic()
    _review_and_open(db, to_open, mode)
    t_grok_open_ms = int((time.monotonic() - _t_grok) * 1000)
    # Split grok/open: grok is the dominant cost; open is fast DB work.
    # We cannot split them without modifying _review_and_open, so we report
    # the combined total as t_grok_ms and leave t_open_ms=0 (open is O(1) DB).
    t_grok_ms = t_grok_open_ms
    t_open_ms = 0  # open_session calls are embedded in _review_and_open

    # S2 + S6: emit scan timing + per-stage breakdown + cache stats into the cycle audit.
    scan_duration_ms = int((time.monotonic() - _scan_start_mono) * 1000)
    cache_total = _cache_hits + _cache_misses
    cache_hit_rate_pct = round(_cache_hits / cache_total * 100, 1) if cache_total else 0.0
    audit.log(db, "scanner", "scan_cycle", entity=f"run:{scan.id}",
              scan_duration_ms=scan_duration_ms,
              t_universe_ms=t_universe_ms,
              t_fetch_ms=t_fetch_ms,
              t_backtest_ms=t_backtest_ms,
              t_votes_ms=t_votes_ms,
              t_grok_ms=t_grok_ms,
              t_open_ms=t_open_ms,
              cache_hits=_cache_hits, cache_misses=_cache_misses,
              cache_hit_rate_pct=cache_hit_rate_pct,
              candidates=len(candidates),
              evaluated=len(to_fetch) - _n_skipped_thin,
              skipped_thin=_n_skipped_thin,
              pre_blocked=_n_pre_blocked)

    # S6: write a compact runtime snapshot so the scanner-stats partial can pull it
    # without a DB join on the audit log (simpler, cheaper).
    runtime.set(db, "scanner_last_stats", json.dumps({
        "scan_id": scan.id,
        "scan_duration_ms": scan_duration_ms,
        "t_universe_ms": t_universe_ms,
        "t_fetch_ms": t_fetch_ms,
        "t_backtest_ms": t_backtest_ms,
        "t_votes_ms": t_votes_ms,
        "t_grok_ms": t_grok_ms,
        "t_open_ms": t_open_ms,
        "cache_hits": _cache_hits,
        "cache_misses": _cache_misses,
        "cache_hit_rate_pct": cache_hit_rate_pct,
        "evaluated": len(to_fetch) - _n_skipped_thin,
        "skipped_thin": _n_skipped_thin,
        "pre_blocked": _n_pre_blocked,
        "universe": len(universe),
    }))

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
    FAIL-OPEN: a symbol absent from the verdict map is treated as endorsed.

    S3: when the circuit-breaker is FROZEN the scan still records candidates (audit
    trail intact) but opens NO sessions — not even semi.  Each suppressed open is
    audited as ``skipped_frozen`` so the UI/logs explain why nothing opened.
    """
    from app.orchestrator import grok  # lazy — avoid import-time coupling

    # S3: check frozen state once for the whole batch.
    breaker_frozen = runtime.is_frozen(db)
    if breaker_frozen and to_open:
        for c in to_open:
            c["cand"].reason = (c["cand"].reason or "") + " | skipped: breaker frozen"
            audit.log(db, "scanner", "skipped_frozen", entity=c["symbol"])
        return

    # Capital sizing (req a): each candidate's session reserves its full projected DCA-ladder
    # cost — computed once here and reused for the batch saturation pre-check, the per-candidate
    # open-gate, and the session's isolated_fund so the ladder never starves mid-way.
    for c in to_open:
        c["need"] = service.projected_ladder_cost(
            c["symbol"], c["entry"], c["distance_pct"], c["max_waves"]
        )

    # S5: read the active fail mode (runtime-editable; default from settings).
    fail_mode: str = runtime.get(db, "kss:grok_scanner_fail_mode") or settings.grok_scanner_fail_mode

    reviews: dict[str, dict] = {}
    # grok_reviewed_symbols: the set that was actually sent to Grok this scan.
    # Symbols NOT in this set have no explicit verdict (batch-cap drop or Grok disabled).
    grok_reviewed_symbols: set[str] = set()

    if to_open and grok.scanner_enabled():
        # B8: skip the LLM call entirely when the concurrent/capital caps are already
        # saturated — if even the cheapest candidate can't open, nothing could even if Grok
        # endorsed everything.
        can, _why = _can_open(db, min(c["need"] for c in to_open))
        if not can:
            audit.log(db, "scanner", "skipped_capped_batch", entity="grok",
                      reason=_why, symbols=len(to_open))
        else:
            # S5 item 2: sort by expectancy descending and cap at _GROK_REVIEW_BATCH_MAX.
            # SECURITY: items payload is numeric/enum-only — no free-text from market data.
            # Fields: symbol (our own key), consensus/win_rate/loss_rate/net_edge/price
            # (all floats from backtest), ta (numeric/enum indicator bundle from ta_bundle).
            sorted_candidates = sorted(to_open, key=lambda c: c.get("net_edge", 0.0), reverse=True)
            batch = sorted_candidates[:_GROK_REVIEW_BATCH_MAX]
            dropped = sorted_candidates[_GROK_REVIEW_BATCH_MAX:]
            if dropped:
                audit.log(db, "scanner", "grok_batch_truncated",
                          kept=len(batch), dropped=len(dropped),
                          dropped_symbols=",".join(c["symbol"] for c in dropped))
            grok_reviewed_symbols = {c["symbol"] for c in batch}
            items = [{
                "symbol": c["symbol"], "consensus": round(c["consensus"], 1),
                "win_rate": round(c["win_rate"], 1), "loss_rate": round(c["loss_rate"], 1),
                "net_edge": round(c["net_edge"], 2), "price": c["entry"],
                "ta": c.get("ta", {}),
            } for c in batch]
            reviews = grok.review_candidates(db, items)

    for c in to_open:
        cand, symbol = c["cand"], c["symbol"]
        verdict = reviews.get(symbol)
        if verdict and not verdict["endorse"]:
            cand.reason = (cand.reason or "") + f" | Grok veto: {verdict['reason']}"
            audit.log(db, "grok", "scanner_veto", entity=symbol, reason=verdict["reason"])
            continue

        # S5 item 3 — fail_mode="closed": a symbol with no explicit endorse verdict
        # (parse failure, timeout, batch-cap drop, or Grok disabled) must NOT open.
        # Under fail_mode="open" (default) the absent-verdict path is treated as endorsed.
        # Interaction with batch-cap: symbols dropped beyond the top-8 have no verdict;
        # under "closed" they are blocked; under "open" they proceed as if endorsed.
        if fail_mode == "closed" and not (verdict and verdict.get("endorse")):
            # Only apply the closed-mode block when the scanner gate is active; if Grok is
            # disabled entirely, fail_mode is irrelevant (no review was attempted).
            if grok.scanner_enabled():
                cand.reason = (cand.reason or "") + " | skipped: grok_unverified (closed mode)"
                audit.log(db, "scanner", "skipped_grok_unverified", entity=symbol,
                          fail_mode="closed",
                          in_batch=symbol in grok_reviewed_symbols)
                continue

        if verdict and verdict.get("reason"):
            cand.reason = (cand.reason or "") + f" | Grok: {verdict['reason']}"
        ok, why = _can_open(db, c["need"])
        if ok:
            cand.session_id = _open_session(
                db, symbol, c["entry"], mode,
                distance_pct=c["distance_pct"], tp_pct=c["tp_pct"], max_waves=c["max_waves"],
                isolated_fund=c["need"],
            )
        else:
            cand.reason = (cand.reason or "") + f" | capped: {why}"
            audit.log(db, "scanner", "skipped_cap", entity=symbol, reason=why)


def _session_lock(s: KssSession) -> float:
    """Capital an ACTIVE session holds against the deployable budget.

    Lend-the-idle-reservation rule (user spec): while a session has filled < 50% of its
    planned ladder it locks only the cash actually deployed (``total_cost``) — the idle
    reservation is freed for new sessions; once it crosses 50% filled it locks its full
    reservation (``isolated_fund``), committed to finishing the averaging-down plan."""
    reserved = s.isolated_fund or 0.0
    used = s.total_cost or 0.0
    if reserved <= 0:
        return used
    return used if used < 0.5 * reserved else reserved


def _can_open(db: Session, new_need: float) -> tuple[bool, str]:
    """Capital-preservation caps: concurrent sessions, deployable budget, min notional.

    Budget = LIVE mark-to-market equity × (100 − ``equity_backup_pct``)% (a backup reserve the
    bot never deploys). Existing sessions consume the budget via ``_session_lock`` (idle
    reservations of lightly-filled sessions are reusable), and ``new_need`` is the candidate's
    projected full-ladder cost. This replaces the old check that summed flat ``scan_fund``
    reservations against static ``account_equity`` — which falsely tripped the cap while real
    cash sat idle (see [[scanner-funding-knobs]])."""
    from app import risk  # lazy: risk → portfolio → models; avoid an import cycle at load

    active = db.query(KssSession).filter(KssSession.status == SESSION_ACTIVE).all()
    if len(active) >= settings.max_concurrent_sessions:
        return False, f"max concurrent {settings.max_concurrent_sessions}"
    equity = risk.account_equity(db)
    budget = equity * (100 - settings.equity_backup_pct) / 100
    locked = sum(_session_lock(s) for s in active)
    if locked + new_need > budget:
        return False, f"vượt ngân sách triển khai (giữ {settings.equity_backup_pct:.0f}% dự phòng)"
    # Min-notional guard is unchanged from the legacy gate: it asks whether the per-session fund
    # is tradeable at all (a coarse dust floor), independent of the new deployed-budget logic.
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
    try:
        stopped_at = datetime.fromisoformat(ts)
        # Normalise to aware UTC so the subtraction is safe regardless of whether
        # the stored string carries tzinfo (B11: migrate away from utcnow).
        if stopped_at.tzinfo is None:
            stopped_at = stopped_at.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    elapsed_min = (datetime.now(timezone.utc) - stopped_at).total_seconds() / 60.0
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
    from datetime import timedelta

    from app.models import Fill

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.loss_streak_window_days)
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
    distance_pct: float | None = None,
    tp_pct: float | None = None,
    max_waves: int | None = None,
    isolated_fund: float | None = None,
) -> int:
    """Open a KSS session using effective (possibly hyperopt-tuned) params.

    ``isolated_fund`` defaults to the projected full-ladder cost (req a) so the reservation
    matches what the ladder will actually consume; callers may pass a precomputed value.

    Defaults resolve from ``settings`` at call time (not at import/definition time)
    so runtime Strategy-tab edits to ``scan_distance_pct`` / ``scan_tp_pct`` /
    ``scan_max_waves`` are always picked up.
    """
    if distance_pct is None:
        distance_pct = settings.scan_distance_pct
    if tp_pct is None:
        tp_pct = settings.scan_tp_pct
    if max_waves is None:
        max_waves = settings.scan_max_waves
    if isolated_fund is None:
        isolated_fund = service.projected_ladder_cost(symbol, entry, distance_pct, max_waves)
    row = service.create_session(
        db,
        symbol=symbol,
        entry_price=entry,
        distance_pct=distance_pct,
        max_waves=max_waves,
        isolated_fund=isolated_fund,
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
