"""The global 200/minute limit must actually bite on the routers app.main includes.

slowapi's middleware looks the handler up in `app.routes` and EXEMPTS a request whose handler it
cannot find. fastapi >= 0.137 wraps every include_router() in an `_IncludedRouter` with no
`.endpoint`, so on those versions the limit silently turns off for every /api, /partials and
/kss route — no error, no log. This test is the tripwire for that kind of dependency bump."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.security import limiter


def test_default_rate_limit_applies_to_an_included_router_endpoint():
    limiter.reset()
    try:
        with TestClient(app) as c:
            codes = [c.get("/api/kss-settings").status_code for _ in range(205)]
    finally:
        limiter.reset()  # never leave the shared in-memory bucket drained for later tests
    assert codes[0] == 200
    assert 429 in codes, "rate limit never triggered — slowapi could not see the route handler"
