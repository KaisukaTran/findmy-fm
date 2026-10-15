"""Values that reach markup through a `|safe` path are escaped at the source (defence in depth:
today's inputs are DB timestamps and allowlisted sort keys, but the sinks must not rely on it)."""

from __future__ import annotations

import pytest

from app import charts
from app.routes import templates

_HOSTILE = '"><img src=x onerror=alert(1)>'


@pytest.mark.parametrize("fn", [charts._time_label, charts._date_time_label])
def test_unparseable_time_label_is_escaped(fn):
    out = fn(_HOSTILE)
    assert "<" not in out and '"' not in out


def test_equity_curve_svg_escapes_a_bad_timestamp():
    svg = charts.equity_curve_svg([1.0, 2.0, 3.0], [_HOSTILE] * 3)
    assert "<img" not in svg


def test_hourly_pnl_svg_escapes_a_bad_label():
    svg = charts.opus_hourly_pnl_svg([_HOSTILE, _HOSTILE], [1.0, -1.0])
    assert "<img" not in svg


def test_positions_partial_escapes_the_sort_query():
    html = templates.get_template("partials/positions.html").render(
        rows=[], page=2, has_prev=True, has_next=True, sort=_HOSTILE, dir="asc&x=<b>",
    )
    assert "<img" not in html and "<b>" not in html
    assert "onerror=alert" not in html.replace("onerror%3Dalert", "")  # only its urlencoded form


def test_positions_partial_keeps_a_clean_sort_query():
    html = templates.get_template("partials/positions.html").render(
        rows=[], page=2, has_prev=True, has_next=True, sort="quantity", dir="desc",
    )
    assert "/partials/positions?page=2&sort=quantity&dir=desc" in html
    assert "/partials/positions?page=3&sort=quantity&dir=desc" in html
