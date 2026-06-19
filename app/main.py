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
from app.config import settings
from app.db import init_db
from app.kss.routes import router as kss_router
from app.routes import api_router, ui_router
from app.security import install_security

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _local_time(timestamp: float | None = None) -> time.struct_time:
    """Log %(asctime)s in the configured display zone (Vietnam GMT+7) regardless of the host TZ."""
    base = datetime.utcfromtimestamp(timestamp) if timestamp is not None else datetime.utcnow()
    return (base + timedelta(hours=settings.tz_offset_hours)).timetuple()


logging.Formatter.converter = staticmethod(_local_time)

_STATIC_DIR = Path(__file__).parent / "static"


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
    # scheduler would never scan, so the two must boot together.
    if settings.scheduler_enabled or settings.full_auto:
        scheduler.start()
    from app import notify, notify_discord
    notify.start()
    notify_discord.start()
    if settings.opus_mode:
        from app.orchestrator import loop as opus_loop

        opus_loop.start()
    try:
        yield
    finally:
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
    return app


app = create_app()
