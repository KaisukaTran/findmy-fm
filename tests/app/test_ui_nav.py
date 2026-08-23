"""P1: sidebar nav / tab-panel structure regression guard
(docs/ui-rebuild-brief.md §4, §13 — 9 tabs -> 8 tab ids, "strategy" is the
new landing tab, "overview"/"trading"/"opus" ids must no longer exist)."""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app

EXPECTED_TAB_IDS = {
    "strategy", "book", "losses", "calendar", "config", "logs", "costs", "savings",
}


@pytest.fixture
def client():
    with TestClient(fastapi_app) as c:
        yield c


def test_nav_has_exactly_the_8_expected_tab_ids(client):
    r = client.get("/")
    assert r.status_code == 200
    nav_tab_ids = set(re.findall(r'data-tab="([a-z]+)"', r.text))
    assert nav_tab_ids == EXPECTED_TAB_IDS


def test_landing_panel_is_strategy_and_marked_active(client):
    r = client.get("/")
    body = r.text

    # The strategy nav button is the one carrying .active / aria-selected="true".
    m = re.search(r'<button class="tab([^"]*)" data-tab="strategy"[^>]*aria-selected="([a-z]+)"', body)
    assert m, "strategy nav button not found"
    classes, aria_selected = m.groups()
    assert "active" in classes.split()
    assert aria_selected == "true"

    # Every other nav button must NOT be marked active/selected.
    for tab_id in EXPECTED_TAB_IDS - {"strategy"}:
        m2 = re.search(rf'<button class="tab([^"]*)" data-tab="{tab_id}"[^>]*aria-selected="([a-z]+)"', body)
        assert m2, f"{tab_id} nav button not found"
        classes2, aria2 = m2.groups()
        assert "active" not in classes2.split(), f"{tab_id} should not be active"
        assert aria2 == "false"

    assert 'data-tab-panel="strategy"' in body


def test_every_data_tab_has_a_matching_data_tab_panel(client):
    r = client.get("/")
    body = r.text
    tab_ids = set(re.findall(r'data-tab="([a-z]+)"', body))
    panel_ids = set(re.findall(r'data-tab-panel="([a-z]+)"', body))
    assert tab_ids == panel_ids


def test_old_tab_ids_are_gone(client):
    r = client.get("/")
    body = r.text
    for stale in ("overview", "trading", "opus"):
        assert f'data-tab="{stale}"' not in body
        assert f'data-tab-panel="{stale}"' not in body
