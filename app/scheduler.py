"""
Background scheduler — drives autonomous operation.

Each cycle: close overdue sessions → check TP on open sessions → scan the
universe (auto-opens sessions in full-auto) → auto-fill KSS orders whose limit
the market reached (full-auto only). Off by default; toggled via settings /
the /api/scheduler endpoint. Everything it does is audit-logged downstream.

`run_cycle(db)` is the synchronous unit of work (unit-testable); the async loop
just calls it on an interval.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
import urllib.parse
import urllib.request

from sqlalchemy.orm import Session

from app import audit, orders, scanner
from app.clock import utcnow
from app.config import settings
from app.db import SessionLocal
from app.kss import service

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_guard_task: asyncio.Task | None = None
# Serialize the 30-min cycle and the fast position-guard so only one DB writer runs at a time
# (prevents a guard exit racing manage_open_sessions on the same session → no double-sell).
_work_lock = threading.Lock()
_last_cycle_at: str | None = None
_last_summary: dict = {}
# 2026-09-03 hang hardening: the 90s guard's last COMPLETED pass — set at the end of
# `_guard_once`, mirroring `_last_cycle_at`'s "only on success" semantics (an exception
# anywhere above skips the stamp, same as run_cycle). `/health` uses this + `_last_cycle_at` to
# report `stalled`, so an OUTSIDE watchdog (data/ensure_live.ps1) can detect a wedged process
# (e.g. a hung ccxt socket call) and restart it even though the app itself never crashed.
_last_guard_at: str | None = None

# The 90s guard's own reconcile pass fetches every tracked order SERIALLY (one weight-4 call
# each) ahead of the hard-SL check, under `_work_lock` — so a large tracked-order count adds
# tens of seconds of latency in front of the exit check the guard exists to run fast. Above
# this count, `_guard_once` skips its own reconcile for that tick (`run_cycle`'s own reconcile,
# every scan_interval_min, still covers everything) rather than block the guard.
GUARD_RECONCILE_MAX_ORDERS = 30

# Cross-process singleton lock. Two app processes each running this scan loop race
# scanner._can_open (separate DB transactions) and blow past max_concurrent_sessions
# (observed 8–9 active vs a cap of 5). A localhost-only socket is a process-wide mutex:
# only ONE process can bind it, and the OS frees it automatically on exit (no stale lock
# files). 8801 = 8000 (app) + a fixed offset reserved for this lock.
_SINGLETON_PORT = 8801
_lock_sock: socket.socket | None = None


def _acquire_singleton_lock(port: int = _SINGLETON_PORT) -> bool:
    """True if this process is the scheduler singleton; False if another already holds it.
    Idempotent: a process that already holds the lock returns True. (`port` is overridable
    for tests.)"""
    global _lock_sock
    if _lock_sock is not None:
        return True
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))  # no SO_REUSEADDR — a 2nd bind MUST fail
        s.listen(1)
    except OSError:
        s.close()
        return False
    _lock_sock = s
    return True


def _release_singleton_lock() -> None:
    global _lock_sock
    if _lock_sock is not None:
        _lock_sock.close()
        _lock_sock = None


def status() -> dict:
    """Lightweight scheduler status for the header badge / /api/automation."""
    return {
        "scheduler_running": is_running(),
        "interval_min": settings.scan_interval_min,
        "last_cycle_at": _last_cycle_at,
        "last_guard_at": _last_guard_at,
        "last_summary": _last_summary,
    }


# --- public-IP change detection (live-readiness gap-closer) ---------------------------------
#
# Binance can restrict an API key to a whitelisted IP; once a whitelist is set, the key stops
# working the instant this machine's public IP changes (e.g. a dynamic ISP lease renewing) —
# exactly the same total-silence failure mode as a dead key (see app.execution's credential
# alerting), just triggered by the network instead of the exchange. This is a CHEAP periodic
# check, not a live-only exchange call: it never touches ccxt/the exchange at all.

RUNTIME_KEY_LAST_PUBLIC_IP = "last_public_ip"
_IP_LOOKUP_URL = "https://api.ipify.org"
_IP_CHECK_INTERVAL_SEC = 300.0  # a few minutes; cheap, but must not run every cycle
_last_ip_check_at: float = 0.0


def reset_ip_check_throttle() -> None:
    """Clear the internal IP-check throttle (tests, and a credentials change)."""
    global _last_ip_check_at
    _last_ip_check_at = 0.0


def fetch_public_ip(timeout: float = 5.0) -> str | None:
    """This machine's current public IP, or None on ANY failure — never raises. A failed lookup
    must never be misread as "the IP changed"; the caller treats None as skip-not-alert."""
    try:
        import requests

        resp = requests.get(_IP_LOOKUP_URL, timeout=timeout)
        resp.raise_for_status()
        ip = resp.text.strip()
        return ip or None
    except Exception:
        return None


def check_ip_change(db: Session, *, lookup=None) -> str | None:
    """Detect a change in the machine's public IP since the last check; alert once when it does.

    Live-only (paper must stay inert — a paper instance has no real key to lose): a no-op when
    `settings.live_trading` is off. Internally throttled to at most once per
    `_IP_CHECK_INTERVAL_SEC` regardless of how often the caller invokes this — the scheduler
    cycle is already minutes apart, but this is a defensive floor in case that ever changes, or
    this is ever called from somewhere more frequent (e.g. the fast position guard). The last
    seen value is persisted via `app.runtime` (survives a restart, so a restart never re-alerts
    on an IP that changed while the process was down but hasn't changed again since).

    NEVER raises: a lookup failure (no network) just skips — it is explicitly NOT read as a
    change, and no alert fires. The first observation (nothing stored yet) only establishes the
    baseline; it is not itself a "change". Returns the new IP when a change was detected and
    alerted, else None.
    """
    global _last_ip_check_at
    if not settings.live_trading:
        return None
    now = time.monotonic()
    if now - _last_ip_check_at < _IP_CHECK_INTERVAL_SEC:
        return None
    _last_ip_check_at = now
    try:
        from app import runtime

        ip = (lookup or fetch_public_ip)()
        if not ip:
            return None  # lookup failed — skip, never alert on that
        last = runtime.get(db, RUNTIME_KEY_LAST_PUBLIC_IP)
        runtime.set(db, RUNTIME_KEY_LAST_PUBLIC_IP, ip)
        if last and last != ip:
            from app import notify

            notify.event(
                "risk",
                f"🌐 Public IP changed: {last} → {ip}. If the LIVE exchange API key has an IP "
                "whitelist, every signed call will now fail until it's updated (placements, "
                "cancels, and the reconciliation that books fills all go silent).",
            )
            return ip
        return None
    except Exception:  # a diagnostic check must never break the cycle
        logger.debug("check_ip_change failed", exc_info=True)
        return None


def _run_periodic(db: Session) -> tuple[int, bool]:
    """Phase C: time-gated per-pair hyperopt + ML retrain. Never raises."""
    from datetime import datetime

    hyperopt_runs = 0
    ml_trained = False
    try:
        from app import hyperopt, ml, runtime
        now = utcnow()

        def _due(key: str, hours: float) -> bool:
            last = runtime.get(db, key)
            if not last:
                return True
            try:
                return (now - datetime.fromisoformat(last)).total_seconds() >= hours * 3600
            except ValueError:
                return True

        if settings.hyperopt_enabled and _due("hyperopt_last_at", settings.hyperopt_interval_hours):
            for sym in settings.watchlist:
                if hyperopt.run_for(db, sym) is not None:
                    hyperopt_runs += 1
            runtime.set(db, "hyperopt_last_at", now.isoformat())
        if settings.ml_enabled and _due("ml_last_at", settings.ml_retrain_hours):
            ml_trained = ml.train(db) is not None
            runtime.set(db, "ml_last_at", now.isoformat())
        # Stage 3: check the volatility-derived target against what closed sessions actually
        # did. Time-gated like the rest — learning on every cycle would chase noise.
        if _due("autotune_learn_last_at", settings.autotune_learn_interval_hours):
            from app import autotune

            autotune.learn_from_outcomes(db)
            runtime.set(db, "autotune_learn_last_at", now.isoformat())
    except Exception:  # periodic tuning must never kill the cycle
        logger.exception("phase-c periodic tasks failed")
    return hyperopt_runs, ml_trained


_HEARTBEAT_ALLOWED_SCHEMES = {"http", "https"}


def _fire_heartbeat_ping(url: str) -> None:
    """Best-effort GET to *url* (a healthchecks.io-style dead-man's switch). Runs on the
    calling thread — `_ping_heartbeat` is what offloads this to a daemon thread. NEVER
    raises: a monitor that is slow, unreachable, or returns an error must not surface here,
    only be noted at debug level.

    Scheme allowlist: `urllib.request.urlopen` happily opens `file://`/`ftp://`/etc, and
    `settings.heartbeat_url` is operator-supplied config, not a validated http(s) endpoint —
    only `http`/`https` are ever requested; anything else is refused with one debug log and
    no request. The response is read+closed via a context manager rather than left to the
    garbage collector (urlopen's return value is a connection that stays open until closed).
    """
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in _HEARTBEAT_ALLOWED_SCHEMES:
        logger.debug("heartbeat ping to %s skipped — unsupported scheme %r", url, scheme)
        return
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 — scheme allowlisted above
            resp.read(1)
    except Exception:
        logger.debug("heartbeat ping to %s failed", url, exc_info=True)


def _spawn_daemon(target, args: tuple = ()) -> None:
    """Run *target* on a new daemon thread. A thin, overridable seam so a test can make the
    call synchronous without touching the global `threading.Thread` (which every other
    thread pool in the process — e.g. the scanner's OHLCV fetch pool — also constructs from)."""
    threading.Thread(target=target, args=args, daemon=True).start()


def _ping_heartbeat() -> None:
    """Fire the configured heartbeat URL (settings.heartbeat_url) in a daemon thread so a
    slow or dead monitor can never block the scheduler loop. No-op when unconfigured."""
    url = settings.heartbeat_url
    if not url:
        return
    _spawn_daemon(_fire_heartbeat_ping, (url,))


def run_cycle(db: Session) -> dict:
    """One scheduler cycle. Returns a small summary (counts), not data dumps."""
    global _last_cycle_at, _last_summary
    from datetime import timedelta

    from app import autotune, circuit, guardian, notify
    from app.models import PENDING, PendingOrder
    # Before anything reads the gates: a configuration that contradicts itself trades NOTHING
    # and says nothing about it, so put it back into a range that can actually open a session.
    tuned = autotune.enforce_consistency(db)
    # Cheap, live-only, self-throttled diagnostic: alert on a public-IP change (the likely
    # cause once a whitelist is set), BEFORE anything below touches the exchange this cycle.
    # Never raises (see check_ip_change); inert on paper.
    check_ip_change(db)
    # Live-only: book fills of resting maker orders the exchange filled since last cycle,
    # BEFORE TP/scan run so sessions/positions reflect reality. No-op on paper.
    reconciled = orders.reconcile_live_orders(db)
    # Straight after reconciliation, while session statuses are fresh: retire rungs still
    # pending for a session that has ended, so the venue never holds an unmanaged BUY.
    service.sweep_orphan_waves(db)
    closed = service.sweep_deadlines(db)
    tp = service.manage_open_sessions(db)
    service.manage_orphan_positions(db)  # TP/SL leftover positions no session/OPUS covers
    # Max-DCA is PULL-only now (folded into /summary + a 'Liệt kê' button) — no proactive push.
    scan: dict
    try:
        scan = scanner.run_scan(db)
    except scanner.ScanInProgress:
        # A manual /api/scan is mid-flight; skip this cycle's scan rather than collide on the
        # SQLite writer. The rest of the cycle (TP, breaker, auto-fill) still runs.
        logger.info("run_cycle: scan already in progress, skipping scan this cycle")
        scan = {"scan_id": None, "candidates": []}
    breaker = circuit.evaluate(db)
    frozen = breaker["frozen"]

    # Veto TTL: expire stale Guardian vetoes so a transient veto can't permanently
    # deadlock a KSS DCA wave whose limit price has since been reached. Cleared orders
    # become auto-eligible again and are re-reviewed below (if the Guardian is on) — if
    # still unsafe they get re-vetoed with a fresh timestamp. Runs unconditionally
    # (even when frozen / Guardian off) so a stuck veto always drains. Legacy rows with
    # no timestamp are treated as already expired.
    veto_expired = 0
    ttl = settings.guardian_veto_ttl_min
    if ttl > 0:
        cutoff = utcnow() - timedelta(minutes=ttl)
        stale = (
            db.query(PendingOrder)
            .filter(
                PendingOrder.status == PENDING,
                PendingOrder.auto_veto == True,  # noqa: E712
                (PendingOrder.auto_veto_at == None) | (PendingOrder.auto_veto_at < cutoff),  # noqa: E711
            )
            .all()
        )
        for order in stale:
            order.auto_veto = False
            order.auto_veto_reason = None
            order.auto_veto_at = None
            audit.log(db, "guardian", "veto_expired", entity=f"order:{order.id}",
                      symbol=order.symbol)
            veto_expired += 1

    # Guardian review: veto any auto-eligible orders the LLM deems unsafe.
    guardian_vetoes = 0
    if not frozen and guardian.enabled():
        _eligible_sources = list(set(settings.autoapprove_sources) | {"kss"})
        pend = (
            db.query(PendingOrder)
            .filter(
                PendingOrder.status == PENDING,
                PendingOrder.auto_veto == False,  # noqa: E712
                PendingOrder.source.in_(_eligible_sources),
                # Guardian only screens NEW risk (BUYs). Exits (SELLs) reduce risk and must
                # never be vetoed — vetoing a take-profit/stop traps capital (drawdown).
                PendingOrder.side == "BUY",
            )
            .all()
        )
        if pend:
            vetoes = guardian.review(pend)
            for oid, reason in vetoes.items():
                order = db.get(PendingOrder, oid)
                if order is not None:
                    order.auto_veto = True
                    order.auto_veto_reason = reason
                    order.auto_veto_at = utcnow()
                    audit.log(db, "guardian", "veto", entity=f"order:{oid}", reason=reason)
                    notify.event("risk", f"⛔ Guardian vetoed order {oid} ({order.symbol}): {reason}")
                    guardian_vetoes += 1

    # Phase C: periodic per-pair hyperopt + ML retrain (time-gated, never blocks).
    hyperopt_runs, ml_trained = _run_periodic(db)

    # Defense-in-depth: short-circuit the auto branches when frozen. The callees
    # also self-guard, but gating here makes the breaker's intent explicit.
    # Live maker model (1.5): rungs queued above rest on the exchange NOW instead of waiting
    # for the market to reach them. Self-guards (no-op on paper / maker off), so it runs
    # unconditionally — cancels must drain even while frozen; placement re-gates per order.
    resting_tp = service.sync_resting_tp(db)
    resting = orders.sync_resting_orders(db)
    filled = orders.auto_fill_due_orders(db) if settings.auto_trade and not frozen else []
    auto_approved = [] if frozen else orders.auto_approve_by_policy(db)  # self-guards on autoapprove_enabled
    audit.log(db, "scheduler", "cycle", deadlines_closed=len(closed), tp_queued=len(tp),
              candidates=len(scan["candidates"]), auto_filled=len(filled),
              auto_approved=len(auto_approved), reconciled=len(reconciled),
              resting_placed=resting["placed"], resting_cancelled=resting["cancelled"],
              resting_tp=resting_tp["queued"] + resting_tp["replaced"], frozen=frozen,
              autotuned=len(tuned),
              guardian_vetoes=guardian_vetoes, veto_expired=veto_expired,
              hyperopt_runs=hyperopt_runs, ml_trained=ml_trained)
    db.commit()
    # Periodic Telegram digest (no-op unless telegram_digest_hours>0 and the interval elapsed).
    try:
        notify.maybe_send_digest(db)
    except Exception:
        logger.debug("maybe_send_digest failed")
    summary = {
        "deadlines_closed": closed,
        "tp_queued": tp,
        "scan_id": scan["scan_id"],
        "auto_filled": filled,
        "auto_approved": auto_approved,
        "reconciled": reconciled,
        "resting": resting,
        "resting_tp": resting_tp,
        "frozen": frozen,
        "guardian_vetoes": guardian_vetoes,
        "veto_expired": veto_expired,
        "hyperopt_runs": hyperopt_runs,
        "ml_trained": ml_trained,
    }
    _last_cycle_at = utcnow().isoformat()
    _last_summary = {k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()}
    # Outbound dead-man's switch (C4): ping an external monitor now that the cycle reached
    # here WITHOUT raising. Placed after every other bookkeeping line in the function so an
    # exception anywhere above (scan, TP, guardian, ...) skips the ping — a failed cycle must
    # stay silent, that silence IS the alert on the monitor's side. Fire-and-forget in a
    # daemon thread: a slow/dead monitor must never hold up the next cycle.
    _ping_heartbeat()
    return summary


def _cycle_once() -> None:
    db = SessionLocal()
    try:
        # Warm the OHLCV candle cache OUTSIDE _work_lock first: the fetch is network-heavy
        # (~minutes on a cold cache after a restart) but read-only w.r.t. session rows, so
        # doing it off-lock keeps the 90s position-guard responsive. run_cycle's own
        # _prefetch_candles then hits the warm cache, so the lock is held only for the fast
        # write phase. Best-effort — on failure run_cycle fetches under the lock as before.
        try:
            scanner.prefetch_universe_candles(db)
        except Exception:
            logger.exception("candle prefetch failed (non-fatal; scan will fetch under lock)")
        with _work_lock:  # never run concurrently with the fast guard
            run_cycle(db)
    finally:
        db.close()


def _guard_reconcile_backlog(db: Session) -> int:
    """Count of tracked (linked, non-terminal) orders reconcile would fetch this pass — the
    same filter ``orders.reconcile_live_orders`` queries. Cheap: one COUNT, no per-order fetch."""
    from app.models import PendingOrder

    return (
        db.query(PendingOrder)
        .filter(
            PendingOrder.exchange_order_id.isnot(None),
            (PendingOrder.exchange_status.is_(None))
            | (PendingOrder.exchange_status.notin_(orders._TERMINAL_EXCHANGE_STATUS)),
        )
        .count()
    )


def _guard_once() -> None:
    global _last_guard_at
    db = SessionLocal()
    try:
        with _work_lock:  # serialize with the 30-min cycle
            # C2: reconcile BEFORE the guard's exit checks — without this, `run_position_guard`
            # sizes hard-SL decisions off `total_filled_qty` that can be a full scan_interval_min
            # stale while rungs fill on the venue between run_cycle's own reconcile calls.
            # Self-gates on live_enabled() (paper no-ops) and is idempotent, so calling it here
            # on top of run_cycle's call is safe. At ~9 tracked orders this is ~36 weight per
            # 90s (fetch_order is weight 4) — bounded below by GUARD_RECONCILE_MAX_ORDERS so a
            # larger backlog cannot add tens of seconds of SERIAL fetch_order latency ahead of
            # the hard-SL check this guard exists to run fast; run_cycle's own reconcile (every
            # scan_interval_min) still covers everything regardless. Any exception is swallowed
            # AND rolled back: a flush/commit-level failure (e.g. "database is locked") leaves
            # the session needing a rollback, and without one `run_position_guard`'s first query
            # would raise PendingRollbackError and kill the entire 90s exit tick — repeatedly.
            try:
                backlog = _guard_reconcile_backlog(db)
                if backlog > GUARD_RECONCILE_MAX_ORDERS:
                    logger.warning(
                        "position-guard reconcile skipped this tick — %d tracked orders exceeds "
                        "GUARD_RECONCILE_MAX_ORDERS (%d); run_cycle's reconcile still covers "
                        "everything", backlog, GUARD_RECONCILE_MAX_ORDERS,
                    )
                else:
                    orders.reconcile_live_orders(db)
            except Exception:
                logger.exception("position-guard reconcile failed")
                try:
                    db.rollback()
                except Exception:
                    logger.exception("position-guard reconcile rollback also failed")
            service.run_position_guard(db)
            _last_guard_at = utcnow().isoformat()
    finally:
        db.close()


async def _loop() -> None:
    logger.info("scheduler started (every %s min)", settings.scan_interval_min)
    while True:
        try:
            # Offload the blocking, network-heavy cycle to a thread so the event
            # loop (and the API) stays responsive.
            await asyncio.to_thread(_cycle_once)
        except Exception:  # a bad cycle must not kill the loop
            logger.exception("scheduler cycle failed")
        await asyncio.sleep(max(settings.scan_interval_min, 1) * 60)


def guard_should_run() -> bool:
    """Whether the fast exit guard runs at all.

    It used to be gated on ``kss_dynamic_tp_enabled``, which defaults to False — so on a
    default install the hard stop-loss, the crash-detect and the freeze-immune exit filler
    never ran, and the only exit check was the 30-minute cycle. That sampling gap is what once
    turned a -15% floor into a -17.3% realised loss, and `run_position_guard`'s own docstring
    calls the hard SL "always-on safety, independent of the dynamic-TP toggle". The dynamic-TP
    parts inside the guard still self-gate on their own flag; the protection does not.

    ``kss_exit_check_sec = 0`` is the deliberate off switch.
    """
    return settings.kss_exit_check_sec > 0


async def _guard_loop() -> None:
    """Fast, lightweight exit guard — decoupled from the 30-min cycle so the hard stop-loss and
    a trailing stop are checked every ``kss_exit_check_sec`` rather than every 30 minutes."""
    logger.info("position-guard started (every %ss)", settings.kss_exit_check_sec)
    while True:
        try:
            if guard_should_run():
                await asyncio.to_thread(_guard_once)
        except Exception:  # a bad guard tick must not kill the loop
            logger.exception("position-guard tick failed")
        await asyncio.sleep(max(settings.kss_exit_check_sec, 5))


def start() -> bool:
    """Start the background loop if not already running. Returns True if started.

    Refuses to start (returns False) when another app process already holds the
    singleton lock — only one process may run the scan loop, or the two race
    scanner._can_open and overshoot max_concurrent_sessions."""
    global _task
    if _task and not _task.done():
        return False
    if not _acquire_singleton_lock(settings.scheduler_lock_port):
        logger.warning(
            "scheduler NOT started — another app instance already holds the singleton lock "
            "(127.0.0.1:%d). Run a single process per lock port, or give a parallel instance a "
            "distinct scheduler_lock_port.", settings.scheduler_lock_port,
        )
        return False
    settings.scheduler_enabled = True
    _task = asyncio.create_task(_loop())
    global _guard_task
    if not (_guard_task and not _guard_task.done()):
        _guard_task = asyncio.create_task(_guard_loop())
    return True


def stop() -> bool:
    """Stop the background loop. Returns True if a running task was cancelled."""
    global _task, _guard_task
    settings.scheduler_enabled = False
    cancelled = False
    if _task and not _task.done():
        _task.cancel()
        cancelled = True
    _task = None
    if _guard_task and not _guard_task.done():
        _guard_task.cancel()
    _guard_task = None
    _release_singleton_lock()  # free the lock so the same process can restart cleanly
    return cancelled


def is_running() -> bool:
    return bool(_task and not _task.done())
