"""Every knob the runtime registry owns must be ACCEPTED by the API that sets it.

The third half of the join `tests/app/test_knob_round_trip.py` guards. That test walks the
template and requires each rendered knob to appear in the payload `app.js` posts. This one
closes the other gap: a field can be in `KSS_SETTING_FIELDS` (so `runtime.set_kss_settings`
casts and persists it) and absent from `KssSettingsBody` (so FastAPI drops it before the handler
ever sees it). The POST then returns 200 with the knob unchanged, and nothing anywhere reports
a problem.

Found the hard way on 2026-09-06: `kss_trail_arm_tp_frac` shipped in the registry but not in the
body, so enabling the newly connected Ride&Trail returned a cheerful 200 and left the feature
switched off. The failure is silent by construction — a success response for a no-op.
"""

from __future__ import annotations

from app.routes import KssSettingsBody
from app.runtime import KSS_SETTING_FIELDS


def test_every_runtime_knob_is_accepted_by_the_settings_endpoint():
    missing = sorted(set(KSS_SETTING_FIELDS) - set(KssSettingsBody.model_fields))
    assert not missing, (
        "these knobs are persisted by the runtime registry but silently dropped by the API "
        f"body, so POSTing them succeeds and does nothing: {missing}"
    )


def test_the_body_does_not_accept_knobs_the_registry_cannot_persist():
    # The reverse direction: a field the endpoint accepts but the registry does not know is
    # applied to the in-memory settings and lost on the next restart.
    extra = sorted(set(KssSettingsBody.model_fields) - set(KSS_SETTING_FIELDS))
    assert not extra, f"accepted by the API but not persisted across a restart: {extra}"
