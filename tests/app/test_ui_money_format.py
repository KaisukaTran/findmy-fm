"""P3 (P-4/P-4b): money/price format guards — docs/ui-rebuild-brief.md §8/§12/§13.

- The new `price` Jinja filter: adaptive precision, always "$"-prefixed, and a
  sub-cent coin never rounds to the unreadable "$0.00".
- Regression guards on the specific P-4/P-4b spots the audit named: `losses.html`/
  `trades.html` fee, `positions.html` avg/current price, `kss.html` trail_sl_price,
  `calendar.html`/`calendar_day.html` day/week/month P&L cells — none of them may
  render a bare `%.4f`/un-prefixed money figure anymore.
- End-to-end: a sub-cent position (e.g. an OSMO/RVN-style $0.0355 / $0.0034 coin)
  renders its real price on `/partials/positions`, not "0.03"/"0.04"/"0.00".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app.main import app as fastapi_app
from app.models import Position
from app.routes import _price

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "templates"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 60000.0))
    with TestClient(fastapi_app) as c:
        yield c


# --- the `price` filter: adaptive precision, always "$"-prefixed ---------


@pytest.mark.parametrize("value,expected", [
    (1234.5, "$1,234.50"),
    (0.5, "$0.5000"),
    (0.0355, "$0.0355"),      # P-4b's own example: must NOT round to "$0.04"
    (0.0034331411166459917, "$0.00343314"),  # RVN-style: must NOT round to "$0.00"
    (0.0000123, "$0.00001230"),
    (0, "$0.00"),
    (None, "$0.00"),
])
def test_price_filter_boundaries(value, expected):
    assert _price(value) == expected


def test_price_filter_always_dollar_prefixed():
    for v in (0, 0.000001, 0.005, 0.5, 1, 1234.5, 999999.99):
        assert _price(v).startswith("$")


def test_price_filter_never_renders_a_visible_zero_for_a_real_subcent_price():
    # The exact P-4b failure mode: a real, nonzero sub-cent price must not display
    # as "$0.00" (which reads as "free"/broken).
    for v in (0.0355, 0.0034331411166459917, 0.00318, 0.0000123):
        rendered = _price(v)
        assert rendered != "$0.00", f"{v} rendered as {rendered!r}"


# --- regression guards: the specific P-4/P-4b spots the audit named ------


def _text(name: str) -> str:
    return (TEMPLATES_DIR / "partials" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("filename", [
    "losses.html", "trades.html", "kss.html", "positions.html",
    "calendar.html", "calendar_day.html", "ladder.html", "pending.html", "savings.html",
])
def test_no_bare_four_decimal_money_format(filename):
    """`"%.4f"|format(...)` (no filter, no "$") was the P-4 fee bug — must be gone
    from every template this phase touched."""
    assert '"%.4f"' not in _text(filename) and "'%.4f'" not in _text(filename)


def test_calendar_day_and_week_cells_are_dollar_prefixed_and_filtered():
    """P-4: calendar day/week/month cells used a bare `%+.2f`/`%+.0f` with no "$"
    while the header total in the SAME file used `${{ ... | money_kmb }}`."""
    text = _text("calendar.html")
    assert "%+.2f" not in text and "%+.0f" not in text
    day_text = _text("calendar_day.html")
    assert "%+.2f" not in day_text


@pytest.mark.parametrize("filename,needle", [
    ("positions.html", "r.avg_entry_price | price"),
    ("positions.html", "r.current_price | price"),
    ("kss.html", "s.trail_sl_price | price"),
    ("losses.html", "r.fee | money"),
    ("trades.html", "r.fee | money"),
    ("opus.html", "d.gross | money"),
    ("opus.html", "d.cost | money"),
    ("opus.html", "d.net | money"),
])
def test_specific_p4_spots_use_the_right_filter(filename, needle):
    assert needle in _text(filename)


# --- P4 item 3: opus.html's daily-table gross/cost/net + the dropped .cards ---


def test_opus_daily_table_no_longer_has_bare_plus_2f_money_cells():
    """P4 item 3: opus.html's gross/cost/net cells used a bare `"%+.2f"`/`"%.2f"`
    with no "$" and no filter — the same P-4 bug pattern already fixed elsewhere."""
    text = _text("opus.html")
    assert '"%+.2f"|format(d.gross)' not in text
    assert '"%.2f"|format(d.cost)' not in text
    assert '"%+.2f"|format(d.net)' not in text


def test_opus_daily_table_renders_dollar_prefixed_money_end_to_end(client, monkeypatch):
    """End-to-end (not just a template-source grep): a real negative-gross day
    renders "$-..." with the money filter's thousands/2dp formatting, not a
    bare signed float."""
    from app.orchestrator import ledger as opus_ledger

    fake_daily = [{
        "day": "2026-08-20", "gross": -1234.5, "cost": 1.5, "net": -1236.0,
        "net_pct": -2.77, "engine_pct": 0.5, "trades": 3, "win_trades": 1,
    }]
    monkeypatch.setattr(opus_ledger, "daily_series", lambda db, days=14: fake_daily)

    r = client.get("/partials/opus")
    assert r.status_code == 200
    row = r.text[r.text.index("2026-08-20"): r.text.index("2026-08-20") + 300]
    assert "$-1,234.50" in row  # gross
    assert "$1.50" in row       # cost
    assert "$-1,236.00" in row  # net
    assert '"%+.2f"' not in row and '"%.2f"' not in row


def test_opus_html_drops_the_cards_kpi_strip():
    """P4 item 3 / brief §8: `.cards` is reserved for the Chiến lược landing tab
    (summary.html) only — every other tab uses a thin summary line instead
    (the kss.html/losses.html pattern). Checks the actual markup, not the
    explanatory HTML comment above it (which necessarily says "cards" in prose)."""
    text = _text("opus.html")
    assert 'class="cards' not in text and "class=\"card" not in text


def test_money_and_money_kmb_filters_are_always_dollar_prefixed_in_templates():
    """Every `| money` / `| money_kmb` call site across the touched templates must
    have a literal "$" immediately before it — the filters themselves don't add
    one (unlike the new `price` filter, which is self-contained)."""
    pattern = re.compile(r"\{\{[^}]*\|\s*money(?:_kmb)?\b[^}]*\}\}")
    offenders = []
    for f in sorted((TEMPLATES_DIR / "partials").glob("*.html")):
        text = f.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            # "$" either right before the {{ }} block, or embedded in the same
            # expression (e.g. `{{ '$' ~ (v | money) }}`).
            start = max(0, m.start() - 3)
            if "$" not in text[start:m.start()] and "$" not in m.group(0):
                offenders.append(f"{f.name}: {m.group(0)}")
    assert offenders == [], "money/money_kmb rendered without a preceding $:\n" + "\n".join(offenders)


# --- end-to-end: a real sub-cent position renders correctly --------------


def test_subcent_position_price_renders_readable_on_positions_partial(client, db, monkeypatch):
    """OSMO/RVN-style regression (P-4b, verified live): a $0.0355 coin must not
    show "0.04", and a $0.0034 coin must not show "0.00"."""
    db.add(Position(symbol="OSMO", quantity=100.0, avg_entry_price=0.03551926722882352))
    db.add(Position(symbol="RVN", quantity=100.0, avg_entry_price=0.0034331411166459917))
    db.commit()
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: {"OSMO": 0.0348, "RVN": 0.00318})

    r = client.get("/partials/positions")
    assert r.status_code == 200

    osmo_row = r.text[r.text.index("OSMO"): r.text.index("OSMO") + 300]
    assert "$0.0355" in osmo_row  # avg (4dp) — not "0.04"
    assert "$0.0348" in osmo_row  # current (4dp) — not "0.03"

    rvn_row = r.text[r.text.index("RVN"): r.text.index("RVN") + 300]
    assert "$0.00343314" in rvn_row  # avg (sub-cent) — not "0.00"
    assert "$0.00318000" in rvn_row  # current (sub-cent) — not "0.00"
