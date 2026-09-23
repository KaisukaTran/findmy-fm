"""
FINDMY-FM application factory (lean rebuild).

    uvicorn app.main:app --reload --port 8000

Wires the database (tables created on startup), security middleware, static
assets, the dashboard, and the JSON + KSS APIs.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.clock import utcnow
from app.config import settings
from app.db import init_db
from app.diagrams import router as diagrams_router
from app.kss.routes import router as kss_router
from app.routes import api_router, ui_router
from app.security import install_security

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx/httpcore log every request at INFO, and the Telegram bot token lives in the request URL
# (api.telegram.org/bot<token>/…) — at INFO that token is written cleartext to the uvicorn logs on
# every poll. Silence their request logging so no secret leaks to disk.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _local_time(timestamp: float | None = None) -> time.struct_time:
    """Log %(asctime)s in the configured display zone (Vietnam GMT+7) regardless of the host TZ."""
    base = datetime.utcfromtimestamp(timestamp) if timestamp is not None else utcnow()
    return (base + timedelta(hours=settings.tz_offset_hours)).timetuple()


logging.Formatter.converter = staticmethod(_local_time)

_STATIC_DIR = Path(__file__).parent / "static"


def ws_feed_should_start() -> bool:
    """Whether the lifespan below starts the Binance public WS price feed: live always starts
    it (subject to ``live_ws_prices``); paper only when the operator opted in via
    ``paper_ws_prices`` (default off = today's behaviour — paper never starts the feed). A pure
    function of ``settings`` so the gate is testable without booting the app or opening a real
    socket. Read once at process start — toggling either knob on the dashboard needs a restart
    to take effect."""
    return settings.live_ws_prices and settings.live_exchange == "binance" and (
        settings.live_trading or settings.paper_ws_prices
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app import runtime, scheduler
    from app.config import settings
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        runtime.sync_from_db(db)
    finally:
        db.close()

    # Log the go-live posture at boot (never logs secrets). Paper unless explicitly armed.
    from app import execution
    live_msg = execution.validate_at_boot()
    if live_msg:
        logging.getLogger("app.main").warning(live_msg)

    # Start the scan loop when it is explicitly enabled OR when full-auto is active
    # (persisted via runtime_config or set in .env) — full-auto without a running
    # scheduler would never scan, so the two must boot together. scheduler.should_run() (not
    # the raw flags) also honours an operator's own "Scheduler off" override, restored by
    # runtime.sync_from_db above — a restart must not silently re-arm a scheduler the operator
    # explicitly stopped just because full_auto is still persisted on (2026-09-23 cross-check).
    if scheduler.should_run():
        started = scheduler.start()
        # 2026-09-22 split-brain outage: scheduler.start() returns False both when this
        # process already runs the loop (harmless, is_running() True) and when ANOTHER
        # process holds the singleton lock (scheduler.is_running() stays False). Only the
        # second case is the outage: a process that should run the scheduler but lost the
        # lock race used to fall through and serve :8001 anyway, with no scheduler at all,
        # for as long as it stayed up (measured ~13.7h). Fail fast instead — refuse to
        # finish startup, so uvicorn exits and an external watchdog can retry cleanly.
        if not started and not scheduler.is_running():
            reason = (
                f"scheduler singleton lock (127.0.0.1:{settings.scheduler_lock_port}) is held "
                "by another process while this process should run the scheduler — refusing to "
                "serve as a scheduler-less twin (2026-09-22 split-brain outage)."
            )
            logging.getLogger("app.main").error(reason)
            if settings.scheduler_lock_fail_fast:
                try:
                    from app import notify

                    notify.event(
                        "risk",
                        "⚠️ Khởi động bị huỷ: không giành được khoá scheduler (một tiến trình "
                        "khác đang giữ). Dừng lại thay vì chạy song sinh KHÔNG có scheduler.",
                    )
                except Exception:
                    pass  # a best-effort alert must never block the fail-fast raise below
                raise RuntimeError(reason)
    # Outage-visibility notice: a process that answers /health can still have sat with a dead
    # scheduler for hours (the 2026-09-22 incident) — the operator's only real signal is the
    # gap between "now" and the last thing the app actually did. Never blocks startup: any
    # failure here (DB not ready, notify down) is swallowed.
    try:
        from app.audit import log as audit_log
        from app.models import AuditLog

        gdb = SessionLocal()
        try:
            last = gdb.query(AuditLog).order_by(AuditLog.created_at.desc()).first()
            now = utcnow()
            gap_min = (now - last.created_at).total_seconds() / 60.0 if last else None
            if gap_min is not None and gap_min > settings.outage_notice_min:
                hours, minutes = divmod(int(round(gap_min)), 60)
                audit_log(
                    gdb, "system", "app_restarted_after_gap",
                    gap_minutes=round(gap_min, 1), last_activity_at=last.created_at.isoformat(),
                )
                gdb.commit()
                from app import notify, timefmt

                # created_at is stored naive-UTC; shift to the configured display zone (Vietnam
                # by default) before printing it in the alert — a UTC timestamp in a Vietnamese
                # message read 7 hours off the actual outage window (2026-09-23 cross-check).
                last_local = timefmt.to_local(last.created_at)
                last_str = last_local.strftime("%H:%M %d/%m") if last_local else "?"
                text = (
                    f"App khởi động lại — sổ không hoạt động {hours} giờ {minutes} phút "
                    f"(từ {last_str})."
                )
                # Fire-and-forget: notify.event does a synchronous HTTP POST (10s timeout) to
                # Telegram/Discord — a slow or unreachable bot must never delay the rest of
                # startup (ws feed, notify pollers, yielding control to serve traffic). Any
                # failure inside the thread is already swallowed by notify.event itself.
                import threading

                threading.Thread(
                    target=notify.event, args=("risk", text), daemon=True
                ).start()
        finally:
            gdb.close()
    except Exception:
        logging.getLogger("app.main").exception("outage-gap notice failed (non-fatal)")
    if ws_feed_should_start():
        from app.data import ws_feed

        ws_feed.start()
        logging.getLogger("app.main").info(
            "ws_feed: %s Binance price stream started",
            "live" if settings.live_trading else "paper",
        )
    from app import notify, notify_discord
    notify.start()
    notify_discord.start()
    if settings.opus_mode:
        from app.orchestrator import loop as opus_loop

        opus_loop.start()
    try:
        yield
    finally:
        try:
            from app.data import ws_feed

            ws_feed.stop()
        except Exception:
            pass
        scheduler.stop()
        notify.stop()
        notify_discord.stop()
        from app.orchestrator import loop as opus_loop

        opus_loop.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="FINDMY-FM",
        version=__version__,
        description="Lean paper-trading simulator with the KSS Pyramid DCA strategy.",
        lifespan=lifespan,
    )
    install_security(app)

    _STATIC_DIR.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(api_router)
    app.include_router(kss_router)
    app.include_router(ui_router)
    app.include_router(diagrams_router)
    return app


app = create_app()
