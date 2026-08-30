"""
Binance is the ONLY supported venue — Kraken was removed entirely (2026-08-30).

Why: every extra ccxt venue is another set of symbol filters, quote-asset
conventions (USD vs USDT) and rate-limit rules we would have to keep compliant
alongside Binance's. Kraken was never used for live trading and only added
config-surface and doc drift (see docs/capital-scaling notes), so it is gone —
both `live_exchange` and `data_exchange` now default to Binance.

This module has two jobs:
1. Assert the new defaults directly.
2. A repo-level guard that greps `app/` for the literal word "kraken" (case-
   insensitive) so a future edit — or a copy-pasted docstring — cannot quietly
   reintroduce the venue without a test noticing.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _PROJECT_ROOT / "app"

_KRAKEN_RE = re.compile("kraken", re.IGNORECASE)


def test_live_exchange_defaults_to_binance():
    """A fresh Settings() (no env override) must default live_exchange to binance."""
    fresh = Settings(_env_file=None)
    assert fresh.live_exchange == "binance"


def test_data_exchange_defaults_to_binance():
    """A fresh Settings() (no env override) must default data_exchange to binance."""
    fresh = Settings(_env_file=None)
    assert fresh.data_exchange == "binance"


def test_app_package_has_zero_kraken_references():
    """Guard: no file under app/ may mention "kraken" (case-insensitive).

    Binance is the only supported venue — this catches a regression (or a
    careless copy-paste of an old docstring/example) before it ships.
    """
    offenders: list[str] = []
    for path in _APP_DIR.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue  # binary asset or similar — not a source of text references
        if _KRAKEN_RE.search(text):
            offenders.append(str(path.relative_to(_PROJECT_ROOT)))
    assert not offenders, f"'kraken' still referenced in: {offenders}"
