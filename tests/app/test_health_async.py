"""I1: `/health` answers off the shared request thread pool.

It used to be a sync route, so Starlette ran it on the same ~40-token anyio pool as every other
sync route. A network blip makes dashboard partials block on ccxt for up to 20 s each, the pool
fills, `/health` queues behind them, and the external watchdog (2 failures 60 s apart -> kill)
restarted an app that was still completing its cycles (7 restarts in 70 min on 16/10). The body
reads only in-memory scheduler state + settings + `execution.credentials_ok()` - no DB, no network,
no lock - so it can run directly on the event loop and never wait for a worker thread.
"""

from __future__ import annotations

import inspect

from fastapi.testclient import TestClient

from app import routes
from app.main import app


def test_health_is_a_coroutine_function():
    # A plain `def` would be dispatched to the thread pool - the thing this fixes.
    assert inspect.iscoroutinefunction(routes.health)


def test_health_touches_no_database(monkeypatch):
    """Proves the body is safe to run on the event loop: with every DB entry point booby-trapped
    after startup, it still answers 200."""

    def boom(*a, **k):
        raise RuntimeError("no db in health")

    with TestClient(app) as c:
        monkeypatch.setattr("app.db.SessionLocal", boom)
        monkeypatch.setattr("app.db.get_db", boom)
        r = c.get("/health")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and "stalled" in body and "credentials_ok" in body
