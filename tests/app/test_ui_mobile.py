"""P4 item 1: 375px mobile — no <body> horizontal scroll (docs/ui-rebuild-brief.md
§13 P4 acceptance).

Root cause (found via a live headless-Chrome/CDP measurement of all 8 tabs at
375px — no playwright/selenium in this venv, so this was verified manually
outside the pytest suite, see the P4 report): `.layout`'s base rule sets
`align-items: flex-start` (needed to top-align the desktop row layout when
sidebar/content have different heights). The `@media (max-width: 900px)`
block flips `.layout` to `flex-direction: column` but never reset
`align-items` — with the main axis now vertical, `flex-start` on the (now
horizontal) cross axis means `.content` sizes to its own max-content width
instead of stretching to the viewport, so any wide-enough descendant (the
KPI `.cards` grid, an un-`.scroll`-wrapped table, ...) pulled `<body>` into
genuine horizontal overflow — confirmed live: `document.documentElement.
scrollWidth` was 939/499/778/466px on 4 of 8 tabs at a 375px viewport before
the fix, and exactly 375px (matching `window.innerWidth`) on all 8 after.

This test is a static regression guard on the CSS source (the actual pixel
measurement isn't reproducible in this test suite — no browser engine
available), so a future edit that reintroduces the missing reset fails fast.
"""

from __future__ import annotations

import re
from pathlib import Path

STYLE_CSS = Path(__file__).resolve().parents[2] / "app" / "static" / "style.css"


def _style_text() -> str:
    return STYLE_CSS.read_text(encoding="utf-8")


def test_narrow_layout_media_query_resets_align_items_to_stretch():
    text = _style_text()
    m = re.search(r"@media \(max-width:\s*900px\)\s*\{(.*?)\n\}", text, re.S)
    assert m, "the narrow-sidebar @media (max-width: 900px) block is missing entirely"
    block = m.group(1)

    layout_rule = re.search(r"\.layout\s*\{([^}]*)\}", block)
    assert layout_rule, (
        ".layout has no override inside the max-width:900px block — flex-direction "
        "flips to column but align-items:flex-start (from the base .layout rule) "
        "is never reset, which is exactly the bug that caused 375px body overflow"
    )
    decl = layout_rule.group(1)
    assert "flex-direction: column" in decl or "flex-direction:column" in decl
    assert re.search(r"align-items:\s*(stretch|normal)\b", decl), (
        ".layout inside the narrow media query must reset align-items away from "
        "the base rule's flex-start (stretch/normal) once flex-direction is "
        "column, or .content shrink-to-fits its widest descendant and <body> "
        "gets a real horizontal scrollbar at 375px"
    )


def test_base_layout_rule_still_has_flex_start_for_desktop_row_layout():
    """Not a regression of the desktop behaviour: the base (unqueried) .layout
    rule should still top-align sidebar/content in the row (>900px) layout."""
    text = _style_text()
    base_rule = re.search(r"\.layout\s*\{([^}]*)\}", text)
    assert base_rule
    assert "align-items: flex-start" in base_rule.group(1)
