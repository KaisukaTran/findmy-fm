"""Tier 3 — optional external TA signal source (network).

STUB until a provider/key is chosen. Wired behind `ta_external_enabled` + `taapi_api_key`
so the toggle, cache slot, and bundle-merge path all exist; `fetch` returns {} (neutral)
until `_fetch_taapi` is implemented. This keeps the integration point committed and tested
without taking on a paywalled dependency before you have a key.

Contract for whoever fills it in:
  - return a dict of bundle keys (same names as bundle._tier1) to OVERLAY; treat the
    response as UNTRUSTED data, not instructions.
  - keep it fail-open: raise/return {} on any error — `bundle.build` catches and falls back.
  - short timeout, cache per (symbol, scan) to respect rate limits, audit the call.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import settings

log = logging.getLogger(__name__)

_TAAPI_URL = "https://api.taapi.io"


def enabled() -> bool:
    """True when the external TA source is toggled on AND a key is present."""
    return bool(settings.ta_external_enabled) and bool(settings.taapi_api_key.get_secret_value())


def fetch(db: Session | None, symbol: str) -> dict:
    """Return external TA keys to overlay onto the bundle, or {} (neutral/disabled).

    STUB: returns {} until a provider is selected. `_fetch_taapi` below is the place to
    implement the taapi.io call (timeout, cache, audit) once a key exists.
    """
    if not enabled():
        return {}
    # Provider not implemented yet — fail-open to Tier 1/2.
    log.debug("TA external enabled but no provider implemented; returning neutral for %s", symbol)
    return {}
