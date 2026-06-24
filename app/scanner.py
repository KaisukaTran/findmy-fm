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
import threading
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

# Parallel OHLCV fetch workers: runtime-configurable via settings.scan_fetch_workers. Each
# worker owns an independent ccxt client (its own rate limiter), so this directly scales the
# burst rate against the exchange's per-IP weight limit — keep it modest.

# Only one scan may run at a time. A full scan holds a long write transaction (hundreds of
# candidates + votes + audit rows); a second concurrent scan — e.g. a manual /api/scan landing
# during the scheduler's cycle — collided on the SQLite writer and raised "database is locked".
# A non-blocking mutex makes the second caller fail fast with ScanInProgress instead.
_scan_lock = threading.Lock()


class ScanInProgress(RuntimeError):
    """Raised by run_scan when another scan already holds the scan lock."""

# The Grok review batch size is the runtime setting ``grok_scanner_batch_max`` (default 60):
# candidates are sorted by expectancy descending and the top N enter the single LLM call. Set it
# high enough to cover every 'trade' candidate so none opens unreviewed. Under fail_mode="closed"
# any symbol beyond the cap has no explicit endorse verdict and does NOT open this scan (same as a
# Grok outage); under fail_mode="open" (default) it opens as before.


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
    ``settings.scan_fetch_workers`` concurrent threads).

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

    with ThreadPoolExecutor(max_workers=max(1, settings.scan_fetch_workers)) as pool:
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


def prefetch_universe_candles(db: Session) -> int:
    """Warm the OHLCV candle cache for the whole universe OUTSIDE the scheduler ``_work_lock``,
    so the slow cold-cache fetch no longer blocks the fast 90s position-guard (which needs the
    same lock). Read-only w.r.t. session/position/order rows — only the in-process candle cache
    is written, and that is independent of any DB row, so warming it off-lock is race-free.

    Uses the SAME ``exchange``/``timeframe``/``limit``/``_universe`` the locked ``run_scan`` uses,
    so the warmed entries match exactly what ``run_scan`` then requests (the universe is a superset
    of run_scan's ``to_fetch``). Best-effort: any failure just leaves ``run_scan`` to fetch under
    the lock exactly as before. Returns the number of symbols warmed."""
    provider = data_provider()
    universe = _universe(db, provider)
    limit = _days_to_bars(settings.backtest_lookback_days, settings.backtest_timeframe)
    _prefetch_candles(settings.data_exchange, universe, settings.backtest_timeframe, limit)
    return len(universe)


def run_scan(db: Session, mode: str | None = None) -> dict:
    """Run one full scan; returns {scan_id, mode, candidates:[...]}.

    Serialised by `_scan_lock`: raises ScanInProgress if another scan is already running
    (prevents two scans colliding on the SQLite writer — 'database is locked')."""
    if not _scan_lock.acquire(blocking=False):
        raise ScanInProgress("a scan is already in progress")
    try:
        return _run_scan_locked(db, mode)
    finally:
        _scan_lock.release()


def _run_scan_locked(db: Session, mode: str | None = None) -> dict:
    _scan_start_mono = time.monotonic()  # S2: wall-clock for scan_duration_ms
    provider = data_provider()
    mode = mode or ("auto" if settings.auto_trade else "semi")

    service.sweep_deadlines(db)  # housekeeping: close anything past its deadline

    # Pre-scan capacity gate (user req): if NO new session can open — the concurrency cap is hit
    # or the deployable budget / 25% backup reserve is exhausted — skip the WHOLE scan. Otherwise
    # we fetch+backtest ~300 coins only to record a wall of "Bỏ qua: vượt ngân sách" Candidate
    # rows + audit lines (junk data + log spam) when nothing can open anyway. TP/SL management
    # earlier in the cycle frees capital, so the next cycle re-checks. Fail-open on a probe error
    # so a transient glitch never silently halts scanning.
    try:
        can_open, why = _has_open_capacity(db)
    except Exception:  # never let the probe block a scan
        can_open, why = True, ""
    if not can_open:
        scan = ScanRun(mode=mode, universe_size=0, params=json.dumps(_thresholds()))
        db.add(scan)
        db.flush()
        audit.log(db, "scanner", "scan_skipped", entity=f"run:{scan.id}", reason=why)
        db.commit()
        return {"scan_id": scan.id, "mode": mode, "candidates": [], "skipped": why}

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

    # BTC reference return for the relative-strength entry gate (computed once; None = gate off/no data)
    _btc_ret = _btc_ref_return(_candle_map, settings.rel_strength_lookback_bars)
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
            # O-COPY/C1: persist the same drawdown evidence the gate trades on, so OPUS
            # can see it too. Pure persistence — does not feed back into any decision here.
            avg_mae=wr["avg_mae"], worst_mae=wr["worst_mae"],
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
            # Hard entry-timing gate: refuse a confirmed downtrend (HTF+ST both down, strong ADX)
            # — don't catch a falling knife. Deterministic mirror of Grok's commonest veto, so it
            # protects entries even when the Grok gate is off. The TA evidence now blocks the open
            # instead of only advising Grok.
            veto = _downtrend_veto(ta)
            knife = None if veto else _falling_knife_veto(ta)
            # Drawdown gate (off when max_avg_mae_pct=0): reject coins whose backtested typical
            # deepest dip below avg is worse than the limit. avg_mae is ≤ 0, so "deeper" = more
            # negative than −max_avg_mae_pct.
            mae_block = None
            if not veto and not knife and settings.max_avg_mae_pct > 0 \
                    and wr["avg_mae"] < -settings.max_avg_mae_pct:
                mae_block = (f"drawdown lịch sử TB {wr['avg_mae']:.1f}% "
                             f"sâu hơn -{settings.max_avg_mae_pct:.0f}%")
            # Relative-strength vs BTC: block coins materially weaker than BTC over the lookback.
            rs_block = None
            if not veto and not knife and not mae_block:
                rs_block = _rel_strength_veto(candles, _btc_ret)
            if veto:
                cand.decision = d["decision"] = "skip"
                cand.reason = (cand.reason or "") + f" | chặn: {veto}"
                audit.log(db, "scanner", "skipped_downtrend", entity=symbol, reason=veto,
                          htf=ta.get("htf"), st=ta.get("st"), adx=ta.get("adx"))
            elif knife:
                cand.decision = d["decision"] = "skip"
                cand.reason = (cand.reason or "") + f" | chặn: {knife}"
                audit.log(db, "scanner", "skipped_entry_timing", entity=symbol, reason=knife,
                          st=ta.get("st"), macd_h=ta.get("macd_h"))
            elif mae_block:
                cand.decision = d["decision"] = "skip"
                cand.reason = (cand.reason or "") + f" | chặn: {mae_block}"
                audit.log(db, "scanner", "skipped_entry_timing", entity=symbol, reason=mae_block,
                          avg_mae=wr["avg_mae"])
            elif rs_block:
                cand.decision = d["decision"] = "skip"
                cand.reason = (cand.reason or "") + f" | chặn: {rs_block}"
                audit.log(db, "scanner", "skipped_rel_strength", entity=symbol, reason=rs_block)
            else:
                # Regime router (docs/pyramid-up-plan.md): tag the candidate's strategy mode
                # from its TA before deferring the open. OFF by default (strategy_router_enabled
                # =False) → always 'dca_down', identical to pre-router behaviour.
                strategy_mode = _route_strategy_mode(ta, candles, _btc_ret)
                # Defer the actual open until after the batched Grok review.
                to_open.append({
                    "cand": cand, "symbol": symbol, "entry": candles[-1]["close"],
                    "distance_pct": distance_pct, "tp_pct": tp_pct, "max_waves": max_waves,
                    "consensus": consensus, "win_rate": wr["win_rate"],
                    "loss_rate": wr["loss_rate"], "net_edge": net_edge,
                    "expectancy": wr["expectancy"], "ta": ta,
                    "win_rate_lb": wr["win_rate_lb"], "trials": wr["trials"],
                    "avg_mae": wr["avg_mae"], "worst_mae": wr["worst_mae"],
                    "strategy_mode": strategy_mode,
                })
        candidates.append(cand)

    # B6: one compact audit entry per scan listing all thin/failed symbols together.
    if _skipped_thin:
        audit.log(db, "scanner", "skipped_thin_data", entity=f"run:{scan.id}",
                  count=len(_skipped_thin), symbols=",".join(_skipped_thin[:20]))

    _t_grok = time.monotonic()
    to_open = _drop_worst_mae_quartile(db, to_open)  # relative drawdown gate (Phase C2)
    _review_and_open(db, to_open, mode, per_scan_cap=_effective_open_cap(db, _candle_map, to_fetch))
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


def _downtrend_veto(ta: dict) -> str | None:
    """Hard entry-timing gate. Returns a reason to SKIP a 'trade' candidate sitting in a
    confirmed downtrend — higher-timeframe trend AND Supertrend both ``down`` with ADX at/above
    ``block_downtrend_adx`` — else None. This is exactly the "htf+st both down strong adx" pattern
    the Grok gate vetoes most often; running it deterministically protects entry timing even when
    Grok is disabled. ``block_downtrend_adx = 0`` disables the gate (back to old behaviour)."""
    adx_min = settings.block_downtrend_adx
    if adx_min <= 0:
        return None
    if ta.get("htf") == "down" and ta.get("st") == "down" and ta.get("adx", 0.0) >= adx_min:
        return f"downtrend khung lớn (HTF+ST down, ADX {ta.get('adx', 0.0):.0f}≥{adx_min:.0f})"
    return None


def _falling_knife_veto(ta: dict) -> str | None:
    """Tighter entry-timing gate than ``_downtrend_veto``. Returns a reason to SKIP a 'trade'
    candidate whose SHORT-TERM momentum is falling — Supertrend ``down`` AND MACD histogram < 0 —
    i.e. don't open a DCA ladder into a coin actively dropping (it would just sit red until a
    bounce). This catches the mild/short-term drops the downtrend gate lets through (that one needs
    HTF+ST+ADX all confirming). Toggle with ``settings.entry_momentum_gate``."""
    if not settings.entry_momentum_gate:
        return None
    if ta.get("st") == "down" and ta.get("macd_h", 0.0) < 0:
        return f"momentum ngắn hạn xuống (ST down, MACDh {ta.get('macd_h', 0.0):+.2f})"
    return None


def _nbar_return(candles: list, n: int) -> float | None:
    """% return over the last ``n`` bars (close[-1] vs close[-1-n]); None if not enough data."""
    if not candles or n <= 0 or len(candles) <= n:
        return None
    prev = candles[-1 - n]["close"]
    return (candles[-1]["close"] / prev - 1) * 100.0 if prev else None


def _btc_ref_return(candle_map: dict, n: int) -> float | None:
    """BTC's ``n``-bar return from the prefetched candle map — the relative-strength benchmark."""
    btc, _ = candle_map.get("BTC", ([], False))
    return _nbar_return(btc, n)


def _rel_strength_veto(coin_candles: list, btc_ret: float | None) -> str | None:
    """Returns a reason to SKIP a coin materially weaker than BTC over the lookback (the 'alt bleeding
    vs BTC' pattern). None when the gate is off, BTC data is missing, or the coin keeps pace. A coin
    UP while BTC is DOWN passes (it outperforms) — so a BTC downtrend alone never blocks a strong alt."""
    if not settings.rel_strength_enabled or btc_ret is None:
        return None
    coin_ret = _nbar_return(coin_candles, settings.rel_strength_lookback_bars)
    if coin_ret is None:
        return None
    if coin_ret < btc_ret - settings.rel_strength_margin_pct:
        n = settings.rel_strength_lookback_bars
        return f"yếu hơn BTC {n}p ({coin_ret:+.1f}% vs BTC {btc_ret:+.1f}%)"
    return None


def _route_strategy_mode(ta: dict, coin_candles: list, btc_ret: float | None) -> str:
    """Regime router (docs/pyramid-up-plan.md): classify a gate-survivor as ``'pyramid_up'``
    (scale into strength) or ``'dca_down'`` (the existing buy-the-dip ladder, default/fallback).
    Behind ``settings.strategy_router_enabled`` (default False = always 'dca_down', zero
    behaviour change). ``rel_strength`` is the coin's N-bar return minus BTC's over the same
    lookback (None btc_ret/coin_ret → treated as 0, which classify_mode then gates on normally —
    a missing relative-strength signal safely falls back to 'dca_down' unless 0 already clears
    the configured threshold)."""
    from app.kss import regime

    coin_ret = _nbar_return(coin_candles, settings.rel_strength_lookback_bars)
    rel_strength = (coin_ret - btc_ret) if (coin_ret is not None and btc_ret is not None) else 0.0
    return regime.classify_mode(
        enabled=settings.strategy_router_enabled,
        htf_trend=ta.get("htf"), st_trend=ta.get("st"), adx=ta.get("adx", 0.0),
        rel_strength=rel_strength, macdh=ta.get("macd_h", 0.0),
        min_rel_strength=settings.pyramid_up_min_rel_strength,
        min_adx=settings.pyramid_up_min_adx,
    )


def _market_breadth(candle_map: dict, symbols: list, n: int) -> float:
    """Fraction (0..1) of the universe whose last-n-bar return is ≥ 0 (rising) — a cheap breadth
    proxy from the already-prefetched candles (no per-coin TA). 0.5 when nothing is measurable."""
    rets = [r for s in symbols if (r := _nbar_return(candle_map.get(s, ([], False))[0], n)) is not None]
    return (sum(1 for r in rets if r >= 0) / len(rets)) if rets else 0.5


def _ramp_factor(breadth: float) -> float:
    """Soft-throttle curve: 0.2 at breadth ≤30%, ramping linearly to 1.0 at breadth ≥60%. Never 0."""
    return 0.2 + 0.8 * max(0.0, min(1.0, (breadth - 0.30) / 0.30))


def _drop_worst_mae_quartile(db: Session, to_open: list[dict]) -> list[dict]:
    """RELATIVE drawdown gate: among the gate-survivors of this scan, drop the worst quartile by
    ``worst_mae`` (deepest single dip below avg). Per-scan/relative, so it never nukes the universe
    like an absolute cutoff (−15%% rejects ~81%%). No-op when off or with <4 candidates."""
    if not settings.mae_quartile_gate_enabled or len(to_open) < 4:
        return to_open
    thr = sorted(c.get("worst_mae", 0.0) for c in to_open)[len(to_open) // 4]  # 25th-pct boundary
    kept = []
    for c in to_open:
        if c.get("worst_mae", 0.0) < thr:  # in the most-negative quartile → drop
            c["cand"].decision = "skip"
            c["cand"].reason = (c["cand"].reason or "") + \
                f" | chặn: worst_mae {c.get('worst_mae', 0.0):.0f}% (quartile sâu nhất)"
            audit.log(db, "scanner", "skipped_mae_quartile", entity=c["symbol"],
                      worst_mae=round(c.get("worst_mae", 0.0), 1))
        else:
            kept.append(c)
    return kept


def _effective_open_cap(db: Session, candle_map: dict, symbols: list) -> int:
    """``max_new_sessions_per_scan`` scaled by market breadth when ``regime_ramp_enabled`` — fewer
    new opens in a broadly-weak market (never a hard stop; ≥1). 0 (unlimited) is left unchanged."""
    base = settings.max_new_sessions_per_scan
    if not settings.regime_ramp_enabled or base <= 0:
        return base
    breadth = _market_breadth(candle_map, symbols, settings.rel_strength_lookback_bars)
    eff = max(1, round(base * _ramp_factor(breadth)))
    if eff < base:
        audit.log(db, "scanner", "regime_ramp", breadth=round(breadth, 2), cap=eff, base=base)
    return eff


def _trade_block_reason(db: Session, symbol: str) -> str | None:
    """Deterministic skip gates for a 'trade' candidate. Returns a reason string to skip,
    or None to proceed. Audits each block."""
    if _in_stop_cooldown(db, symbol):
        audit.log(db, "scanner", "skipped_cooldown", entity=symbol)
        return "stop-loss cooldown"
    if _has_pending_sell(db, symbol):
        audit.log(db, "scanner", "skipped_pending_sell", entity=symbol)
        return "exit (SELL) in flight"
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


def _open_rank_key(c: dict) -> tuple:
    """Best-first ranking key for opens AND the Grok batch (kept identical so Grok reviews exactly
    the candidates that will open). Sorted descending.

    Leads with CONSENSUS — the market-context score {trend,dip,volatility,liquidity,ml} that
    actually varies across coins (≈46–80) — because the backtest metrics saturate: a 4%-TP DCA
    with a 30-day deadline "wins" ~100% of historical trials for nearly every liquid coin, so
    win_rate_lb≈93% and expectancy≈tp−cost are near-constant and cannot rank. ``worst_mae`` (the
    single worst adverse excursion across trials, signed ≤ 0) then breaks ties toward shallower-tail
    (safer) entries — it discriminates far better than ``avg_mae`` (which is compressed); win_rate_lb
    and expectancy are last-resort tiebreaks."""
    return (
        c.get("consensus", 0.0),
        c.get("worst_mae", 0.0),
        c.get("win_rate_lb", 0.0),
        c.get("expectancy", 0.0),
    )


def _review_and_open(
    db: Session, to_open: list[dict], mode: str, per_scan_cap: int | None = None
) -> None:
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
            # Rank EXACTLY like the open loop (_open_rank_key: consensus → avg_mae → win_rate_lb →
            # expectancy) and cap at the runtime grok_scanner_batch_max, so Grok reviews the SAME
            # top candidates that will actually be opened. The backtest metrics saturate in a long
            # lookback (most pairs tie at ~100% win / tp−cost), so leading with consensus keeps the
            # batch order meaningful and aligned with what opens (under fail_mode="open" unreviewed
            # pairs still open).
            # SECURITY: items payload is numeric/enum-only — no free-text from market data.
            # Fields: symbol (our own key), consensus/win_rate/loss_rate/net_edge/price
            # (all floats from backtest), ta (numeric/enum indicator bundle from ta_bundle).
            batch_max = settings.grok_scanner_batch_max
            sorted_candidates = sorted(to_open, key=_open_rank_key, reverse=True)
            batch = sorted_candidates[:batch_max]
            dropped = sorted_candidates[batch_max:]
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

    # Open best-first (_open_rank_key): within the concurrent/per-scan caps, prefer the highest
    # market-context consensus, then the shallowest backtest drawdown (avg_mae) — rather than the
    # saturated win_rate_lb/expectancy or universe (volume) order. When the gate floods with
    # look-alike candidates this is what actually decides which ones get capital.
    ranked = sorted(to_open, key=_open_rank_key, reverse=True)
    # Cap NEW opens per scan (0 = no limit) so exposure ramps gradually.
    if per_scan_cap is None:
        per_scan_cap = settings.max_new_sessions_per_scan
    opened = 0
    for c in ranked:
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
        # Defense-in-depth: re-assert the per-symbol cap atomically against the CURRENT DB
        # state. The to_open pre-check ran earlier (before Grok review and other opens in this
        # same batch); a stale read there must never let a 2nd ladder open on a coin — two
        # sessions share one Position avg → 'take-profit that realizes a loss' / K-2 TP deadlock.
        if _symbol_at_cap(db, symbol):
            cand.reason = (cand.reason or "") + " | capped: per-symbol"
            audit.log(db, "scanner", "skipped_concentration", entity=symbol)
            continue
        # Per-scan ramp cap: once this scan has opened its quota, defer the rest to the next
        # cycle (they remain ranked best-first, so the strongest open first).
        if per_scan_cap and opened >= per_scan_cap:
            cand.reason = (cand.reason or "") + f" | hoãn: đạt cap {per_scan_cap} phiên/scan"
            audit.log(db, "scanner", "skipped_per_scan_cap", entity=symbol, cap=per_scan_cap)
            continue
        ok, why = _can_open(db, c["need"])
        if ok:
            cand.session_id = _open_session(
                db, symbol, c["entry"], mode,
                distance_pct=c["distance_pct"], tp_pct=c["tp_pct"], max_waves=c["max_waves"],
                isolated_fund=c["need"], strategy_mode=c.get("strategy_mode", "dca_down"),
            )
            opened += 1
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


def _has_open_capacity(db: Session) -> tuple[bool, str]:
    """True if at least one NEW KSS session could plausibly open right now — reuses ``_can_open``
    against a representative full-ladder cost (the same scan params every new session uses). Lets
    the scanner skip the WHOLE cycle when capital is saturated (concurrency cap hit OR the
    deployable budget / equity_backup_pct reserve is exhausted) instead of fetch+backtesting the
    whole universe only to record a wall of 'vượt ngân sách' skips."""
    probe = service.projected_ladder_cost(
        "PROBE", 1.0, settings.scan_distance_pct, settings.scan_max_waves
    )
    return _can_open(db, probe)


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


def _has_pending_sell(db: Session, symbol: str) -> bool:
    """True if the symbol has a SELL order still in flight (queued but not yet filled). Opening a
    new session while an exit is mid-flight re-enters a coin that is being closed AND blends its
    Position avg before the SELL realizes — the BICO race: a re-open landed ~1s before the SL fill,
    so the fill-time stop_cooldown could not catch it. Block until the SELL settles."""
    from app.models import PENDING, PendingOrder

    return (
        db.query(PendingOrder.id)
        .filter(PendingOrder.symbol == symbol, PendingOrder.side == "SELL",
                PendingOrder.status == PENDING)
        .first()
        is not None
    )


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
    strategy_mode: str = "dca_down",
) -> int:
    """Open a KSS session using effective (possibly hyperopt-tuned) params.

    ``isolated_fund`` defaults to the projected full-ladder cost (req a) so the reservation
    matches what the ladder will actually consume; callers may pass a precomputed value.

    Defaults resolve from ``settings`` at call time (not at import/definition time)
    so runtime Strategy-tab edits to ``scan_distance_pct`` / ``scan_tp_pct`` /
    ``scan_max_waves`` are always picked up.

    ``strategy_mode`` (docs/pyramid-up-plan.md) is the regime-router tag — NOTE this is a
    SEPARATE axis from ``mode`` (auto/manual, the existing param): 'dca_down' (default, the
    existing frozen buy-the-dip ladder) or 'pyramid_up' (anti-martingale scale-into-strength,
    only ever produced by the router when ``settings.strategy_router_enabled``).
    """
    if distance_pct is None:
        distance_pct = settings.scan_distance_pct
    if tp_pct is None:
        tp_pct = settings.scan_tp_pct
    if max_waves is None:
        max_waves = settings.scan_max_waves
    if isolated_fund is None:
        isolated_fund = service.projected_ladder_cost(symbol, entry, distance_pct, max_waves)

    if strategy_mode == "pyramid_up":
        row = service.create_pyramid_up_session(
            db, symbol=symbol, entry_price=entry, tp_pct=tp_pct,
            deadline_days=settings.deadline_days, note=f"auto-scanner ({mode})",
        )
        started = service.start_pyramid_up_session(db, row.id)
    else:
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
    audit.log(db, "scanner", "session_open", entity=f"kss:{row.id}", symbol=symbol, mode=mode,
              strategy_mode=strategy_mode)

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
                try:
                    orders.approve_order(db, oid, reviewer="auto-trader")
                    audit.log(db, "auto-trader", "auto_approve", entity=f"order:{oid}",
                              symbol=symbol, session=row.id)
                except orders.InsufficientCashError:
                    # No cash to fund even wave 0 — leave it pending; it fills when cash frees.
                    audit.log(db, "scanner", "open_underfunded", entity=f"order:{oid}",
                              symbol=symbol, session=row.id)
        else:
            audit.log(db, "scanner", "auto_skip_frozen",
                      entity=f"order:{started['pending_order_id']}", symbol=symbol, session=row.id)
    return row.id
