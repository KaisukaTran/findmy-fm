"""Every knob the Strategy tab renders must actually reach the server.

The audit of 2026-09-04 named this failure class: the template renders an input, the server
knows the field, the runtime registry casts it — and the payload builder in `app.js` never
sends it, so the control is real, reachable, and inert. Each half is individually correct, so
no unit test sees anything wrong; the join is what is missing. `use_exchange_balance` has been
in that state (rendered, never sent), and `placement_alert_after` plus the ladder-reserve knob
were both caught in it during the 2026-09-01 hardening round before shipping.

This is the structural guard for that class: walk the template's form inputs, and require each
one that the runtime registry owns to appear in the payload `app.js` posts.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.runtime import KSS_SETTING_FIELDS

_APP = Path(__file__).resolve().parents[2] / "app"
_TEMPLATE = _APP / "templates" / "partials" / "kss_settings.html"
_APP_JS = _APP / "static" / "app.js"

# Knobs measured INERT on 2026-09-05: rendered on the Strategy tab, owned by the runtime
# registry, and absent from every payload `app.js` posts. Editing any of them in the dashboard
# silently does nothing. `use_exchange_balance` is the O-2 case from the 2026-09-04 audit; the
# other thirteen were found by this guard the day it was written — including
# `max_session_deploy_usd`, whose own tooltip calls it the hard capital-preservation wall for
# live, and the whole `autotune_*` block (which is why toggling the learner off in the UI on
# 2026-09-04 appeared to have no effect).
#
# They are listed rather than fixed because two of them need a decision, not a line of code:
# `autotune_tp_atr_mult` / `autotune_dca_atr_mult` are written BY the machine, so posting a
# form rendered minutes ago would quietly overwrite what autotune has since learned. Wiring
# them is only safe together with making them read-only, or re-reading before submit.
#
# The list may only ever SHRINK. Delete an entry when the knob is wired; this guard then holds
# it wired forever.
_KNOWN_UNWIRED = {
    "use_exchange_balance",
    "max_session_deploy_usd",
    "intraday_max_bars",
    "autotune_enabled",
    "autotune_levels_enabled",
    "autotune_learn_enabled",
    "autotune_learn_interval_hours",
    "autotune_tp_atr_mult",
    # autotune_dca_atr_mult: WIRED 2026-09-05 (Kai-approved fix; the learner never writes it,
    # so a stale form cannot clobber a machine-learned value — unlike tp_atr_mult above).
    "loss_reentry_enabled",
    "loss_reentry_blacklist_after",
    "loss_reentry_pardon",
    "loss_reentry_weeks_1",
    "loss_reentry_weeks_2",
}


def _rendered_knob_names() -> set[str]:
    html = _TEMPLATE.read_text(encoding="utf-8")
    names = set(re.findall(r'<(?:input|select)\b[^>]*\bname="([^"]+)"', html))
    return {n for n in names if n in KSS_SETTING_FIELDS}


def test_every_rendered_knob_is_in_the_payload_app_js_posts():
    js = _APP_JS.read_text(encoding="utf-8")
    missing = sorted(
        name for name in _rendered_knob_names() - _KNOWN_UNWIRED
        if not re.search(rf'^\s*{re.escape(name)}\s*:', js, re.MULTILINE)
    )
    assert not missing, (
        "rendered on the Strategy tab but never sent, so editing them does nothing: "
        f"{missing}"
    )


def test_the_new_fee_knobs_round_trip_through_the_registry_and_the_form():
    """The two knobs added for fee realism, pinned end to end: registry, template, payload."""
    for name in ("simulated_fee_pct", "fee_safety_margin_pct"):
        assert name in KSS_SETTING_FIELDS, f"{name} missing from the runtime registry"
        assert f'name="{name}"' in _TEMPLATE.read_text(encoding="utf-8")
        assert re.search(rf'^\s*{name}\s*:', _APP_JS.read_text(encoding="utf-8"), re.MULTILINE)


def test_the_known_unwired_list_never_grows_silently():
    """A knob may be exempted only while it is genuinely broken — this pins the debt exactly,
    so nobody quiets a fresh failure by appending to the list. Shrinking it is the only edit
    this test permits without a deliberate change here."""
    assert len(_KNOWN_UNWIRED) == 13, (
        "the inert-knob list changed: shrink it by WIRING a knob, never by adding one"
    )


def test_every_exempted_knob_is_actually_still_inert():
    """Keeps the exemption list honest: an entry that has since been wired must be deleted,
    or the guard silently stops protecting it."""
    js = _APP_JS.read_text(encoding="utf-8")
    stale = sorted(
        name for name in _KNOWN_UNWIRED
        if re.search(rf'^\s*{re.escape(name)}\s*:', js, re.MULTILINE)
    )
    assert not stale, f"wired after all — remove from _KNOWN_UNWIRED: {stale}"
