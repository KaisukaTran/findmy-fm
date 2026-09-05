"""
Dashboard UI contract checks (Task: dashboard polish — env identity + data-k hooks).

- The <title> and body class must carry the machine-readable env ("paper" |
  "testnet" | "real") so paper (:8000) and live (:8001) are never confused, and
  real money gets an unmistakable visual frame (see .env-real in style.css).
- Every polled numeric cell that is "worth noticing" carries a stable data-k
  key (symbol/id — never a loop index) so a sibling tick-flash script can diff
  old vs new text across an htmx swap.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app import models
from app.config import settings
from app.main import app as fastapi_app

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def client():
    with TestClient(fastapi_app) as c:
        yield c


def test_dashboard_title_and_body_class_carry_paper_env(client):
    settings.live_trading = False
    r = client.get("/")
    assert r.status_code == 200
    assert "<title>FINDMY-FM · PAPER</title>" in r.text
    assert 'class="env-paper"' in r.text


def test_dashboard_title_and_body_class_carry_live_testnet_env(client):
    settings.live_trading = True
    settings.live_use_testnet = True
    r = client.get("/")
    assert r.status_code == 200
    assert "<title>FINDMY-FM · LIVE · TESTNET</title>" in r.text
    assert 'class="env-testnet"' in r.text


def test_dashboard_body_carries_env_real_for_live_money(client):
    settings.live_trading = True
    settings.live_use_testnet = False
    r = client.get("/")
    assert r.status_code == 200
    assert 'class="env-real"' in r.text


def test_positions_row_carries_stable_data_k(client, db, monkeypatch):
    """data-k must be derived from the symbol, never a loop index — so a
    tick-flash script can track "does BTC's own price cell change" across
    re-ordered/paginated polls."""
    monkeypatch.setattr(
        portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 100.0)
    )
    db.add(models.Position(symbol="BTC", quantity=1.0, avg_entry_price=90.0, total_cost=90.0))
    db.commit()
    r = client.get("/partials/positions")
    assert r.status_code == 200
    body = r.text
    assert 'data-k="pos:BTC:price"' in body
    assert 'data-k="pos:BTC:upnl"' in body
    assert 'data-k="pos:BTC:qty"' in body
    assert 'data-k="pos:BTC:value"' in body


def test_summary_cards_carry_stable_data_k(client):
    r = client.get("/partials/summary")
    assert r.status_code == 200
    body = r.text
    for key in ("equity", "cash", "market_value", "realized", "unrealized", "trades", "pending"):
        assert f'data-k="sum:{key}"' in body


# --- F2: data-v (raw unformatted value) alongside data-k -------------------
# Cells that render two numbers in one string (e.g. "$40.58 (+2.03%)") used to
# defeat the tick-flash text parser entirely. The fix carries the raw float
# separately in data-v so app.js never has to parse the rendered text.


def test_positions_row_carries_data_v_alongside_data_k(client, db, monkeypatch):
    monkeypatch.setattr(
        portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 100.0)
    )
    db.add(models.Position(symbol="BTC", quantity=1.0, avg_entry_price=90.0, total_cost=90.0))
    db.commit()
    r = client.get("/partials/positions")
    assert r.status_code == 200
    body = r.text
    # data-v must sit on the same element as data-k (same tag), carrying a
    # plain float — not the formatted "$..." / "...%" text.
    m = re.search(r'data-k="pos:BTC:upnl"\s+data-v="(-?[0-9.]+)"', body)
    assert m, body
    assert float(m.group(1)) == pytest.approx(10.0)  # (100 - 90) * 1.0


def test_summary_card_carries_data_v_alongside_data_k(client):
    r = client.get("/partials/summary")
    assert r.status_code == 200
    body = r.text
    m = re.search(r'data-k="sum:equity"\s+data-v="(-?[0-9.]+)"', body)
    assert m, body
    float(m.group(1))  # must parse as a plain number


# --- F7: a kss: data-k on a rendered KSS session row ------------------------


def test_kss_row_carries_stable_data_k_and_data_v(client, db, monkeypatch):
    """kss:<id>:wave / kss:<id>:upnl must be present (and carry a raw data-v)
    so the tick-flash script can diff a session row across the 10s poll."""
    monkeypatch.setattr("app.kss.pyramid.get_current_prices", lambda syms: {"BTC": 100.0})
    s = models.KssSession(
        symbol="BTC", entry_price=100.0, distance_pct=2.0, max_waves=5,
        isolated_fund=1000.0, tp_pct=3.0, timeout_x_min=30.0, gap_y_min=5.0,
        status=models.SESSION_ACTIVE,
    )
    db.add(s)
    db.commit()
    r = client.get("/partials/kss")
    assert r.status_code == 200
    body = r.text
    assert f'data-k="kss:{s.id}:wave"' in body
    assert re.search(rf'data-k="kss:{s.id}:upnl"\s+data-v="-?[0-9.]+"', body), body


# --- F7: CSP contract — no inline style= anywhere in app/templates ---------


def test_no_inline_style_attribute_in_any_template():
    templates_dir = _REPO_ROOT / "app" / "templates"
    offenders = [
        str(path.relative_to(_REPO_ROOT))
        for path in templates_dir.rglob("*.html")
        if 'style="' in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"inline style= found in: {offenders}"


# --- F7: the .env-real inset-shadow rule (real-money frame) exists ---------


def test_env_real_inset_shadow_rule_in_stylesheet():
    css_text = (_REPO_ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
    assert "body.env-real" in css_text
    assert "box-shadow: inset 0 0 0 3px var(--danger);" in css_text


def test_dashboard_asset_urls_carry_a_cache_busting_version():
    """A browser holding the previous app.js keeps running it across a server restart — a
    shipped JS change that silently does not take effect. The URL must change with the file."""
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    body = TestClient(fastapi_app).get("/").text
    assert "/static/app.js?v=" in body
    assert "/static/style.css?v=" in body
    # The stamp must be a real value, not an empty template variable.
    import re

    stamps = re.findall(r"/static/(?:app\.js|style\.css)\?v=(\d+)", body)
    assert len(stamps) == 2 and all(int(s) > 0 for s in stamps)


def _dashboard_html() -> str:
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    return TestClient(fastapi_app).get("/").text


def test_manual_order_session_and_preview_dialogs_are_gone():
    """The three header buttons and their modals were removed on request. Assert the markup
    is actually gone rather than merely hidden, so a half-deleted dialog cannot linger."""
    body = _dashboard_html()
    for gone in ("toggleOrder", "toggleKss", "togglePreview",
                 'x-show="orderOpen"', 'x-show="kssOpen"', 'x-show="previewOpen"',
                 'id="order-form"', 'id="kss-form"', 'id="preview-form"',
                 'id="preview-output"', 'x-data="ui"'):
        assert gone not in body, f"leftover after removal: {gone}"


def test_the_rest_of_the_header_and_the_dashboard_still_render():
    """Removing those dialogs must not take anything else with it: the environment badge, the
    API-key indicator, the sidebar tabs and the capital panel host all still have to be there."""
    body = _dashboard_html()
    assert 'id="key-indicator"' in body
    assert 'data-tab="overview"' in body and 'data-tab="strategy"' in body
    assert 'hx-get="/partials/capital"' in body
    assert 'id="ladder-modal"' in body          # the one modal that stays
    assert 'x-data="autoPopover"' not in body   # that one lives in the status partial
    assert "<body" in body


def test_the_manual_order_and_session_apis_still_exist():
    """Only the UI was removed. The HTTP endpoints stay: they are the emergency manual path on
    a live-money instance, and `queue_order` / `create_session` have internal callers."""
    from app.main import app as fastapi_app

    paths = {r.path for r in fastapi_app.routes}
    assert "/api/orders" in paths
    assert "/api/kss/sessions" in paths
    assert "/api/kss/preview" in paths
