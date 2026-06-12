"""Technical-analysis layer for the scanner gate.

A 3-tier source of TA evidence, all converging on a single compact `bundle.build()`:
  Tier 1 (always on) — pure-Python `indicators` (no numpy/pandas, offline-safe).
  Tier 2 (optional)  — `lib`, a lazy pandas-ta adapter that gracefully falls back to Tier 1.
  Tier 3 (optional)  — `external`, a network signal source (taapi.io), fail-open.

The bundle enriches the evidence handed to the Grok endorse/veto gate so its verdict is
informed; it never changes the deterministic short-listing or the hard cage.
"""

from app.ta.bundle import build

__all__ = ["build"]
