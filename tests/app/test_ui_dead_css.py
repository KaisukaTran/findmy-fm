"""P4 item 2: dead-CSS guards (docs/ui-rebuild-brief.md P-7 / §13 P4 acceptance).

Two things:

1. The hyperopt/ml feature-removal (commit 630c684) left CSS selectors behind
   that no template references — `.statusbar-hyperopt`/`.statusbar-ml` and
   `.badge.hyperopt-off`/`.badge.ml-off` (grepped: zero hits in
   app/templates/ and app/static/app.js) — these must be GONE from
   style.css. `.badge.hyperopt-on`/`.badge.ml-on` are NOT dead — they're
   reused as generic badge colors by live features (opus.html's SHADOW/parse
   -error badges; positions/trades/savings' "OPUS"/"KAI" source tag) — they
   must stay, with the style.css comment explaining why.

2. (The generally useful half.) Every literal `class="..."` token rendered by
   a template should resolve to a selector actually defined in style.css —
   a class with no CSS rule is either a typo (broken styling) or dead
   markup. This is necessarily a heuristic over Jinja templates (a class
   attribute can be built from `{{ ... }}` expressions), so it:
   - always checks the STATIC (non-Jinja) tokens in a class="..." attribute;
   - best-effort checks string-literal OUTPUT branches inside `{{ }}` (e.g.
     `{{ 'ml-on' if x else 'active' }}`), while excluding literals that are
     clearly comparison operands / dict keys / containment checks (e.g.
     `x == 'BUY'`, `'LIVE' in mode.label`, `d.get('expectancy', 0)`) —
     those aren't rendered as classes at all;
   - resolves a dynamic PREFIX (e.g. `class="cat-{{ category }}"` — the
     static remainder "cat-") by checking at least one concrete class with
     that prefix exists (`cat-trade`, `cat-risk`, ... do);
   - never matches Alpine `:class="..."` bindings (a bare Alpine property
     reference, not a literal class list — see test_ui_csp.py's CSP-build
     note) — the `:` lookbehind exclusion below is deliberate.
   A small, explicit, commented allowlist covers the pre-existing (not
   introduced by, and out of scope for, this P4 pass) unstyled classes this
   heuristic still finds — see ALLOWLISTED_UNSTYLED_CLASSES.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "static"
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "templates"
STYLE_CSS = STATIC_DIR / "style.css"

# --- 1. removed hyperopt/ml feature CSS ------------------------------------

REMOVED_SELECTORS = [
    ".statusbar-hyperopt",
    ".statusbar-ml",
    ".badge.hyperopt-off",
    ".badge.ml-off",
]
KEPT_GENERIC_BADGES = [
    ".badge.hyperopt-on",
    ".badge.ml-on",
]


def _style_text() -> str:
    return STYLE_CSS.read_text(encoding="utf-8")


def test_removed_hyperopt_ml_selectors_are_gone():
    # strip comments first: style.css explains the hyperopt-on/ml-on KEEP
    # decision in prose that mentions the removed selector names by name.
    text = re.sub(r"/\*.*?\*/", "", _style_text(), flags=re.S)
    offenders = [sel for sel in REMOVED_SELECTORS if sel in text]
    assert offenders == [], (
        "dead CSS from the removed hyperopt.py/ml.py features (630c684) is still "
        f"present and unreferenced by any template: {offenders}"
    )


def test_hyperopt_on_and_ml_on_are_kept_as_generic_badges():
    """Regression guard the OTHER direction: don't let a future cleanup pass
    delete these — they're live (opus.html SHADOW/parse-error badges;
    positions/trades/savings' OPUS/KAI source tag), just badge-color reuse
    of an old feature's naming."""
    text = _style_text()
    for sel in KEPT_GENERIC_BADGES:
        assert sel in text, f"{sel} should still exist — it's reused by a live feature"


def test_no_template_still_references_the_removed_hyperopt_ml_classes():
    removed_tokens = {"hyperopt-off", "ml-off", "statusbar-hyperopt", "statusbar-ml"}
    offenders = []
    for f in sorted(TEMPLATES_DIR.rglob("*.html")):
        text = f.read_text(encoding="utf-8")
        for tok in removed_tokens:
            if tok in text:
                offenders.append(f"{f.name}: {tok}")
    offenders += [f"app.js: {tok}" for tok in removed_tokens if tok in (STATIC_DIR / "app.js").read_text(encoding="utf-8")]
    assert offenders == [], offenders


# --- 2. every rendered class resolves to a defined selector (or is allowlisted) --

# Pre-existing (not introduced by this P4 pass), harmless, out-of-scope-for-P4
# unstyled classes this heuristic finds. Each is commented with why it's here
# rather than fixed: fixing them is unrelated to P4's four assigned items
# (mobile 375px scroll, dead hyperopt/ml CSS, opus.html money/.cards, final
# sweep) and touches files (costs.html, savings.html, dashboard.html header)
# outside that scope.
ALLOWLISTED_UNSTYLED_CLASSES = {
    # dashboard.html modal close buttons: styled entirely via `.sm.ghost` +
    # the `.modal-header` flex row: the class carries no CSS of its own.
    "modal-close",
    # opus.html wrapper span: a semantic/test hook only (test_ui_partials.py
    # asserts this literal string is present in the /partials/opus response;
    # it isn't `.statusbar` — that's a different, styled class).
    "statusbar-opus",
    # costs.html: highlights the "this is the current period" row — pre-existing
    # gap (costs.html untouched by the UI rebuild so far), out of scope for P4.
    "cost-current",
    # costs.html / savings.html narrow-input width utilities, analogous to the
    # existing (styled) `.w-5em` — pre-existing gap, out of scope for P4.
    "w-6em",
    "w-7em",
    "w-10em",
    "w-12em",
}

_CLASS_ATTR_RE = re.compile(r'(?<!:)class="([^"]*)"')  # excludes Alpine :class="..."
_JINJA_BLOCK_RE = re.compile(r"\{\{.*?\}\}")
# a quoted literal plus a few characters of context on each side, so callers
# can tell a ternary OUTPUT branch (`'x' if y else 'z'`) from a comparison
# operand / dict key / containment check (`y == 'x'`, `.get('x', 0)`, `'x' in y`).
_LIT_WITH_CONTEXT_RE = re.compile(r"(?P<pre>[\w=!.(,]{0,3})\s*['\"](?P<val>[A-Za-z][\w-]*)['\"]\s*(?P<post>[\w)]{0,3})")


def _css_defined_classes() -> set[str]:
    text = re.sub(r"/\*.*?\*/", "", _style_text(), flags=re.S)  # strip comments
    selector_blocks = re.findall(r"([^{}]+)\{", text)
    classes: set[str] = set()
    for block in selector_blocks:
        classes.update(re.findall(r"\.([A-Za-z_][\w-]*)", block))
    return classes


def _is_comparison_or_call_context(pre: str, post: str) -> bool:
    if pre.endswith("==") or pre.endswith("!=") or pre.endswith("(") or pre.endswith(","):
        return True  # `x == 'lit'` / `.get('lit', ...)` — not a rendered class
    if post.startswith("in") or post.startswith(")"):
        return True  # `'lit' in x` / trailing call-close — not a rendered class
    return False


def _classes_used_by_templates() -> tuple[set[str], set[str]]:
    """Returns (concrete_tokens, dynamic_prefixes) rendered via class="..." across
    all templates. `dynamic_prefixes` are static remainders ending in "-" from a
    pattern like `class="cat-{{ category }}"` (checked for a family match, not
    an exact one — see _prefix_has_a_match)."""
    concrete: set[str] = set()
    prefixes: set[str] = set()
    for f in sorted(TEMPLATES_DIR.rglob("*.html")):
        text = f.read_text(encoding="utf-8")
        for m in _CLASS_ATTR_RE.finditer(text):
            raw = m.group(1)
            static_only = _JINJA_BLOCK_RE.sub(" \x00 ", raw)
            for tok in static_only.split():
                if tok == "\x00":
                    continue
                (prefixes if tok.endswith("-") else concrete).add(tok)
            for jinja_expr in re.findall(r"\{\{(.*?)\}\}", raw):
                for mm in _LIT_WITH_CONTEXT_RE.finditer(jinja_expr):
                    if _is_comparison_or_call_context(mm.group("pre"), mm.group("post")):
                        continue
                    concrete.add(mm.group("val"))
    return concrete, prefixes


def _prefix_has_a_match(prefix: str, css_classes: set[str]) -> bool:
    return any(c.startswith(prefix) for c in css_classes)


def test_every_rendered_class_resolves_to_a_defined_css_selector_or_is_allowlisted():
    css_classes = _css_defined_classes()
    concrete, prefixes = _classes_used_by_templates()

    unresolved_prefixes = sorted(p for p in prefixes if not _prefix_has_a_match(p, css_classes))
    assert unresolved_prefixes == [], (
        f"class prefixes with NO matching CSS family at all: {unresolved_prefixes}"
    )

    missing = sorted(concrete - css_classes)
    assert missing == sorted(ALLOWLISTED_UNSTYLED_CLASSES), (
        "Either a genuinely unresolved class appeared (fix the template or add the "
        "CSS rule), or an allowlisted one is now stale (remove it from "
        "ALLOWLISTED_UNSTYLED_CLASSES).\n"
        f"found missing: {missing}\nallowlist: {sorted(ALLOWLISTED_UNSTYLED_CLASSES)}"
    )
