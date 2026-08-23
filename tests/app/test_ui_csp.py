"""P1: CSP + template hygiene guards (docs/ui-rebuild-brief.md §9/§15).

- Zero inline style= attributes anywhere in app/templates.
- Zero <script> tags with a body (only <script defer src="/static/...">).
- Zero onclick=/onload= (or any other inline event-handler attribute).
- The CSP response header is unchanged from app/security.py.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import _CSP

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "templates"

_STYLE_ATTR_RE = re.compile(r'style\s*=\s*"')
_INLINE_EVENT_ATTR_RE = re.compile(r'\son[a-z]+\s*=\s*"', re.IGNORECASE)


def _all_template_files():
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def test_no_inline_style_attributes():
    offenders = []
    for f in _all_template_files():
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if _STYLE_ATTR_RE.search(line):
                offenders.append(f"{f}:{i}: {line.strip()}")
    assert offenders == [], "inline style= found:\n" + "\n".join(offenders)


def test_no_inline_script_bodies():
    offenders = []
    for f in _all_template_files():
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"<script\b[^>]*>", text, re.IGNORECASE):
            tag = m.group(0)
            if "src=" in tag.lower():
                continue  # <script defer src="..."> — allowed
            # any <script> tag without src= is an inline script definition,
            # regardless of whether it currently has a body.
            offenders.append(f"{f}: inline <script> without src= -> {tag}")
    assert offenders == [], "inline <script> found:\n" + "\n".join(offenders)


def test_no_inline_event_handler_attributes():
    offenders = []
    for f in _all_template_files():
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if _INLINE_EVENT_ATTR_RE.search(line):
                offenders.append(f"{f}:{i}: {line.strip()}")
    assert offenders == [], "inline onclick=/onload=/etc. found:\n" + "\n".join(offenders)


@pytest.fixture
def client():
    with TestClient(fastapi_app) as c:
        yield c


def test_csp_header_unchanged(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["Content-Security-Policy"] == _CSP


# --- Alpine CSP build: only property PATHS and METHOD REFERENCES evaluate ----
# `alpine.min.js` here is the CSP build (3.14.1). It refuses to evaluate any
# expression — an assignment (`open = !open`), a call (`open.toString()`), or an
# object literal (`{ 'cls': open }`) in an Alpine attribute is silently INERT.
# This shipped three dead controls before it was caught by hand in a browser
# (the three modal ✕ buttons used `@click="orderOpen=false"` and never closed;
# a sub-view toggle used `@click="open = !open"` and never opened). Tests never
# saw it because the markup renders fine — only the behaviour is missing.
# So: state lives in an `Alpine.data()` component with real methods (app.js),
# and templates may only bind a bare path or a method name.
_ALPINE_ATTR_RE = re.compile(r'(?:@[a-z]+(?:\.[a-z]+)*|x-(?:show|text|html|model)|:[a-z-]+)="([^"]*)"')
# a bare dotted path or a method name — nothing else is safe under the CSP build
_SAFE_ALPINE_VALUE_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")


def test_no_alpine_expressions_the_csp_build_cannot_evaluate():
    offenders = []
    for f in _all_template_files():
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for value in _ALPINE_ATTR_RE.findall(line):
                if value and not _SAFE_ALPINE_VALUE_RE.match(value.strip()):
                    offenders.append(f"{f.name}:{i}: {value!r}")
    assert offenders == [], (
        "Alpine's CSP build evaluates only property paths / method references; "
        "these bindings are inert at runtime — move the logic into an "
        "Alpine.data() component in app/static/app.js:\n" + "\n".join(offenders)
    )
