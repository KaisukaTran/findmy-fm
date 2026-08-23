"""P1: every /partials/* endpoint returns 200 on an empty DB and contains a
characteristic string (regression guard for the P1 template/route rework —
see docs/ui-rebuild-brief.md §12/§13)."""

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app.main import app as fastapi_app


@pytest.fixture
def client(monkeypatch):
    # Avoid any live price-fetch network call from a read view.
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 60000.0))
    with TestClient(fastapi_app) as c:
        yield c


# endpoint -> (path, characteristic substring expected in the response body)
PARTIALS = [
    ("/partials/costs", "costs-pane"),
    ("/partials/savings", "savings-pane"),
    ("/partials/summary", "Total equity"),
    ("/partials/positions", "positions-pane"),
    ("/partials/trades", "trades-pane"),
    ("/partials/pending", "pending-pane"),
    ("/partials/status", "conn-chip"),
    ("/partials/losses", "Tổng lỗ đã chốt"),
    ("/partials/lossautopsy", "Session có exit"),
    ("/partials/live-trading", "live-box"),
    ("/partials/calendar", "cal-head"),
    ("/partials/calendar/day?d=2026-08-23", "cal-day-panel"),
    ("/partials/kss-settings", "kss-settings-form"),
    ("/partials/ladder", "chưa có session"),
    ("/partials/opus", "statusbar-opus"),
    ("/partials/kss", "kss-pane"),
    ("/partials/scanner", "Quét ngay"),
    ("/partials/scanner-stats", "Chưa có dữ liệu quét"),
    ("/partials/performance", 'id="perf"'),
    ("/partials/scan-funnel", 'id="scan-funnel"'),
    ("/partials/audit", "audit-pane"),
]


@pytest.mark.parametrize("path,needle", PARTIALS, ids=[p for p, _ in PARTIALS])
def test_partial_returns_200_with_characteristic_string(client, path, needle):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:300]}"
    assert needle in r.text, f"{path}: expected {needle!r} in response body"


def test_all_ui_router_partial_routes_are_covered():
    """Guard against silently adding/removing a /partials/* route without
    updating PARTIALS above (mechanical set-equality check)."""
    routes = {
        r.path
        for r in fastapi_app.routes
        if getattr(r, "path", "").startswith("/partials/") and "GET" in (getattr(r, "methods", None) or [])
    }
    tested = {p.split("?")[0] for p, _ in PARTIALS}
    assert routes == tested, f"missing from PARTIALS: {routes - tested}; stale in PARTIALS: {tested - routes}"
