"""P2: settings-form coverage regression (docs/ui-rebuild-brief.md §7 R1/R2 —
the P-1 capital-safety bug). The old JS submit handler hand-listed ~64 field
names while the template rendered 92 inputs, so 24 knobs — including
`max_session_deploy_usd`, the per-session capital wall — were silently dropped:
the UI reported "saved" and the value never changed.

Every set below is RECOMPUTED from the current template/schema/app.js — nothing
here is a hard-coded count, so this stays true after either file changes.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.portfolio as portfolio
from app.main import app as fastapi_app
from app.routes import KssSettingsBody

_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = _ROOT / "app" / "templates" / "partials" / "kss_settings.html"
_APP_JS = _ROOT / "app" / "static" / "app.js"

_NAME_RE = re.compile(r'name="(\w+)"')
_CONSENSUS_FIELDS = {"trend", "dip", "volatility", "liquidity"}


def _template_text() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def _schema_fields() -> dict[str, object]:
    return KssSettingsBody.model_fields


def _extract_form(html: str, form_id: str, *, with_tag: bool = False) -> str:
    """Body of the form (or the whole element when ``with_tag``). Attribute-tolerant:
    the opening tag carries `novalidate`, and may grow more attributes later."""
    m = re.search(rf'<form id="{re.escape(form_id)}"[^>]*>(.*?)</form>', html, re.S)
    assert m, f'<form id="{form_id}"> not found'
    return m.group(0) if with_tag else m.group(1)


# --- 1) template names minus schema fields == exactly the 4 consensus fields ---


def test_template_names_minus_schema_is_exactly_the_consensus_weight_fields():
    """Any `name="..."` rendered anywhere in kss_settings.html that is NOT a
    KssSettingsBody field must be one of the 4 fields belonging to the
    deliberately-separate ConsensusWeightsBody schema (its own form/handler) —
    any other stray name is a bug (R2)."""
    template_names = set(_NAME_RE.findall(_template_text()))
    schema_names = set(_schema_fields())
    extra = template_names - schema_names
    assert extra == _CONSENSUS_FIELDS


# --- 2) every KssSettingsBody field has exactly one rendered input (gap == 0) ---


def test_every_kss_settings_body_field_has_exactly_one_rendered_input():
    names = _NAME_RE.findall(_template_text())
    counts: dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    schema_names = set(_schema_fields())
    missing = sorted(f for f in schema_names if f not in counts)
    duplicated = sorted(f for f in schema_names if counts.get(f, 0) > 1)
    assert missing == [], f"KssSettingsBody fields with NO rendered input: {missing}"
    assert duplicated == [], f"KssSettingsBody fields rendered more than once: {duplicated}"


# --- 3) every input inside #kss-settings-form has a varname span + tooltip ---


def test_every_settings_input_has_varname_and_nonempty_tooltip():
    form = _extract_form(_template_text(), "kss-settings-form")
    # one <label ...>...</label> block per field (non-greedy across the block).
    # The attrs group must be quote-aware: several tooltips contain a literal
    # '>' (e.g. ">0 = ...", "rớt > % này", "cắm >12%") which a naive `[^>]*`
    # would treat as the end of the tag and truncate the match.
    labels = re.findall(r"<label\b((?:[^>\"]|\"[^\"]*\")*)>(.*?)</label>", form, re.S)
    assert labels, "no <label> blocks found in #kss-settings-form"
    missing_varname = []
    missing_tooltip = []
    for attrs, body in labels:
        name_m = re.search(r'name="(\w+)"', body)
        if not name_m:
            continue  # not a field label (shouldn't happen, but be defensive)
        field_name = name_m.group(1)
        if 'class="varname"' not in body:
            missing_varname.append(field_name)
        title_m = re.search(r'title="([^"]*)"', attrs)
        if not title_m or not title_m.group(1).strip():
            missing_tooltip.append(field_name)
    assert missing_varname == [], f"fields missing <span class=\"varname\">: {missing_varname}"
    assert missing_tooltip == [], f"fields missing a non-empty tooltip: {missing_tooltip}"


# --- 4) structural: the handler must NOT hand-list per-field keys (the P-1 shape) ---


def _handler_block(js_text: str, form_id: str) -> str:
    m = re.search(
        rf'form\.id === "{re.escape(form_id)}"\)\s*\{{(.*?)\n  \}} else if',
        js_text,
        re.S,
    )
    assert m, f"could not locate the submit handler block for {form_id!r} in app.js"
    return m.group(1)


def test_kss_settings_handler_has_no_hardcoded_field_list():
    block = _handler_block(_APP_JS.read_text(encoding="utf-8"), "kss-settings-form")
    schema_names = set(_schema_fields())
    # A hand-written list calls f.get("field_name") (or similarly reads
    # el/f by a literal field name) once per field — that is exactly the P-1
    # shape (24 fields fell out of sync with the template). The rewritten
    # handler walks form.elements/FormData generically and contains none of
    # these per-field literals.
    literal_field_refs = re.findall(r'"(\w+)"', block)
    offenders = sorted(set(literal_field_refs) & schema_names)
    assert offenders == [], (
        "kss-settings-form handler still references individual schema field "
        f"names by literal (the P-1 hand-listed pattern): {offenders}"
    )
    # And it must actually walk the form generically (FormData / form.elements),
    # not just "happen" to have zero field literals for some other reason.
    assert "form.elements" in block or "FormData(form)" in block


# --- 5) TestClient round-trip: build the payload the way the rewritten handler
# does (walk every rendered field, coerce per its data-type), nudge every value
# to a new-but-still-valid one, POST once, and confirm ALL of them persisted.
# This is the automated stand-in for the manual browser acceptance test
# (docs/ui-rebuild-brief.md §13 P2) and would have caught the historic P-1 bug:
# canary fields below (max_session_deploy_usd, loss_reentry_enabled,
# regime_gate_enabled, max_new_sessions_per_scan, ...) were part of the 24
# silently-dropped knobs / the missing input. ---

# Attrs groups are quote-aware: several tooltips contain a literal '>'
# (e.g. ">0 = ...", "rớt > % này", "cắm >12%") which a naive `[^>]*` would
# mistake for the end of the tag and truncate the match.
_TAG_ATTRS = r'(?:[^>"]|"[^"]*")*'
_INPUT_RE = re.compile(rf"<input\b({_TAG_ATTRS})>")
_SELECT_RE = re.compile(rf"<select\b({_TAG_ATTRS})>(.*?)</select>", re.S)
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_OPTION_RE = re.compile(rf"<option\b({_TAG_ATTRS})>", re.S)


def _attrs(raw: str) -> dict[str, str]:
    return dict(_ATTR_RE.findall(raw))


def _parse_rendered_fields(form_html: str) -> dict[str, tuple[str, str]]:
    """Return {name: (current_value, data_type)} for every input/select in the
    rendered form fragment, exactly as a form submit would see them."""
    out: dict[str, tuple[str, str]] = {}
    for m in _INPUT_RE.finditer(form_html):
        a = _attrs(m.group(1))
        name = a.get("name")
        if not name:
            continue
        out[name] = (a.get("value", ""), a.get("data-type", ""))
    for m in _SELECT_RE.finditer(form_html):
        a = _attrs(m.group(1))
        name = a.get("name")
        if not name:
            continue
        body = m.group(2)
        selected = None
        for om in _OPTION_RE.finditer(body):
            if "selected" in om.group(1):
                selected = _attrs(om.group(1)).get("value")
        if selected is None:
            first = _OPTION_RE.search(body)
            if first:
                selected = _attrs(first.group(1)).get("value")
        out[name] = (selected or "", a.get("data-type", ""))
    return out


def _coerce(raw_value: str, dtype: str):
    if dtype == "bool":
        return raw_value == "1"
    if dtype == "num":
        return float(raw_value)
    return raw_value


def _numeric_bounds(field) -> tuple[float | None, float | None, float | None, float | None]:
    ge = gt = le = lt = None
    for m in field.metadata:
        if hasattr(m, "ge") and m.ge is not None:
            ge = m.ge
        if hasattr(m, "gt") and m.gt is not None:
            gt = m.gt
        if hasattr(m, "le") and m.le is not None:
            le = m.le
        if hasattr(m, "lt") and m.lt is not None:
            lt = m.lt
    return ge, gt, le, lt


def _type_args(field) -> tuple:
    return typing.get_args(field.annotation) or (field.annotation,)


def _nudge(old, field):
    """A new value for *old* that still satisfies the field's own Pydantic
    constraints (so the POST validates — proving the change stuck, not just
    that some arbitrary payload bounced with a 422)."""
    args = _type_args(field)
    if bool in args:
        return not bool(old)
    if str in args:
        base = str(old)
        return base + "_t" if not base.endswith("_t") else base + "x"
    is_int = int in args
    ge, gt, le, lt = _numeric_bounds(field)
    old = float(old)
    step = 1.0 if is_int else (0.1 if abs(old) < 5 else 1.0)
    up = old + step
    if (le is None or up <= le) and (lt is None or up < lt):
        return int(up) if is_int else round(up, 4)
    down = old - step
    if (ge is None or down >= ge) and (gt is None or down > gt):
        return int(down) if is_int else round(down, 4)
    lo = ge if ge is not None else (gt if gt is not None else old - 1)
    hi = le if le is not None else (lt if lt is not None else old + 1)
    mid = (lo + hi) / 2
    return int(mid) if is_int else round(mid, 4)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(portfolio, "get_current_prices", lambda syms: dict.fromkeys(syms, 60000.0))
    with TestClient(fastapi_app) as c:
        yield c


def test_post_round_trip_covers_every_field_in_the_kss_settings_form(client):
    schema_fields = _schema_fields()

    html = client.get("/partials/kss-settings").text
    form_html = _extract_form(html, "kss-settings-form")
    rendered = _parse_rendered_fields(form_html)

    # KssSettingsBody also backs 2 OTHER forms in the same partial (live-exec,
    # grok-fail-mode) that keep their own save actions (docs/ui-rebuild-brief.md
    # §7 R5) — their fields are legitimately absent from #kss-settings-form.
    # Compute that set from the live HTML too, rather than hard-coding it.
    other_form_fields: set[str] = set()
    for other_id in ("live-exec-form", "grok-fail-mode-form"):
        other_html = _extract_form(html, other_id)
        other_form_fields |= set(_parse_rendered_fields(other_html))

    main_form_schema_fields = {n for n in rendered if n in schema_fields}
    # sanity: the main form covers every schema field NOT owned by the other
    # two forms (belt-and-suspenders with test 2 above, using the LIVE rendered
    # HTML rather than a static read)
    assert main_form_schema_fields == set(schema_fields) - other_form_fields

    from app.config import settings

    payload: dict[str, object] = {}
    expected: dict[str, object] = {}
    snapshot: dict[str, object] = {}
    for name, (raw_value, dtype) in rendered.items():
        if name not in schema_fields:
            continue
        assert dtype in {"num", "bool", "str"}, f"{name}: missing/unknown data-type={dtype!r}"
        old = _coerce(raw_value, dtype)
        new = _nudge(old, schema_fields[name])
        payload[name] = new
        expected[name] = new
        snapshot[name] = getattr(settings, name)

    # min_expectancy_pct/scan_tp_pct are cross-field guarded (routes.py
    # set_kss_settings: expectancy can never exceed tp - round-trip cost, or
    # the scanner silently skips 100% of the universe forever). A full-form
    # submit (R1) always includes both, so — independent of what values are
    # picked — they must stay a satisfiable pair. Keep scan_tp_pct's generic
    # nudge, but pin min_expectancy_pct comfortably under the resulting
    # ceiling instead of nudging it blindly.
    if "min_expectancy_pct" in payload and "scan_tp_pct" in payload:
        import app.costengine as costengine

        ceiling = costengine.expectancy_ceiling_pct(payload["scan_tp_pct"])
        safe_min_e = round(min(ceiling - 0.5, 1.0), 2)
        payload["min_expectancy_pct"] = safe_min_e
        expected["min_expectancy_pct"] = safe_min_e

    try:
        r = client.post("/api/kss-settings", json=payload)
        assert r.status_code == 200, r.text
        got = client.get("/api/kss-settings").json()
        mismatches = {n: (expected[n], got.get(n)) for n in expected if got.get(n) != expected[n]}
        assert mismatches == {}, f"fields that did NOT persist after POST: {mismatches}"
    finally:
        # These fields aren't all in conftest's _MUTABLE_SETTINGS allow-list —
        # restore the in-process singleton by hand so this test can't leak
        # state into a later test in the same pytest run.
        for name, value in snapshot.items():
            setattr(settings, name, value)


# --- The silent-no-save trap (found live in P2 review) -----------------------
# `opus_max_trade_notional` shipped as `step="10" min="1"` while its live value
# was 100 — an invalid HTML5 control. The browser then refuses to fire `submit`
# AT ALL, and because the fields sit inside collapsed <details> it cannot even
# show which control is at fault: clicking "Lưu cấu hình KSS" did nothing, with
# no POST, no toast and no error. Three guards so it cannot come back.

_NUMBER_INPUT_RE = re.compile(r"<input([^>]*type=\"number\"[^>]*)>")


def _attr(attrs: str, name: str) -> str | None:
    m = re.search(rf'{name}="([^"]*)"', attrs)
    return m.group(1) if m else None


def test_settings_form_is_novalidate_so_submit_always_reaches_the_handler():
    form = _extract_form(_template_text(), "kss-settings-form", with_tag=True)
    opening = form[: form.index(">") + 1]
    assert "novalidate" in opening, (
        "the KSS settings form must be novalidate: otherwise one invalid control "
        "silently blocks the submit event and 'Lưu' becomes a no-op"
    )


def test_submit_handler_reports_invalid_fields_instead_of_no_opping():
    js = _APP_JS.read_text(encoding="utf-8")
    handler = js[js.index('form.id === "kss-settings-form"'):]
    handler = handler[: handler.index('form.id === "live-exec-form"')]
    for needle in ("checkValidity", "reportValidity", "details"):
        assert needle in handler, f"settings submit handler must {needle}() invalid input"


def test_money_inputs_do_not_declare_a_coarse_step():
    """A money field with step="50"/"10" makes ordinary values invalid (100 is not
    1+10k), which silently kills the whole save. Steps finer than 1 are fine."""
    offenders = []
    for m in _NUMBER_INPUT_RE.finditer(_template_text()):
        attrs = m.group(1)
        name, step = _attr(attrs, "name"), _attr(attrs, "step")
        if not name or step in (None, "any"):
            continue
        if not any(tag in name for tag in ("_usd", "notional", "fund", "volume")):
            continue
        try:
            coarse = float(step) > 1
        except ValueError:
            coarse = True
        if coarse:
            offenders.append(f"{name}: step={step}")
    assert offenders == [], (
        'money inputs must not declare a step coarser than 1 (use step="any"):\n'
        + "\n".join(offenders)
    )


def test_rendered_values_satisfy_their_own_input_constraints(client):
    """Whatever the server renders must be a VALID control value — otherwise the
    browser rejects the form before the handler ever sees it."""
    html = client.get("/partials/kss-settings").text
    offenders = []
    for m in _NUMBER_INPUT_RE.finditer(html):
        attrs = m.group(1)
        name, step, mn, val = (_attr(attrs, k) for k in ("name", "step", "min", "value"))
        if not name or step in (None, "any") or not val:
            continue
        try:
            steps = (float(val) - float(mn or 0)) / float(step)
        except ValueError:
            continue
        if abs(steps - round(steps)) > 1e-9:
            offenders.append(f"{name}={val} violates min={mn} step={step}")
    assert offenders == [], "rendered settings values are invalid controls:\n" + "\n".join(offenders)
