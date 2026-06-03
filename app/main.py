"""
FINDMY-FM application factory (lean rebuild).

    uvicorn app.main:app --reload --port 8000

Wires the database (tables created on startup), security middleware, static
assets, the dashboard, and the JSON + KSS APIs.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.db import init_db
from app.kss.routes import router as kss_router
from app.routes import api_router, ui_router
from app.security import install_security

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


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
