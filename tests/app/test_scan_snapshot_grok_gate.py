"""``ScanRun.params`` recorded the raw ``grok_scanner_enabled`` knob, not the gate that decides.

``grok.scanner_enabled()`` is the knob AND a non-empty ``xai_api_key``. Live has the knob on with
no key configured, so every scan row on live recorded a gate that never ran — the same lie
``acaec7f`` already fixed for ``/api/automation``, left standing here.
"""

from __future__ import annotations

from pydantic import SecretStr

from app.config import settings
from app.scanner import _scan_snapshot


def test_snapshot_records_effective_gate_alongside_the_raw_knob(monkeypatch):
    monkeypatch.setattr(settings, "grok_scanner_enabled", True)
    monkeypatch.setattr(settings, "xai_api_key", SecretStr(""))  # the live posture: no key

    snap = _scan_snapshot(None, None)

    assert snap["grok_scanner_enabled"] is True, "the raw knob must stay visible"
    assert snap["grok_scanner_active"] is False, "no key means the gate never ran"


def test_snapshot_active_true_when_knob_and_key_both_present(monkeypatch):
    monkeypatch.setattr(settings, "grok_scanner_enabled", True)
    monkeypatch.setattr(settings, "xai_api_key", SecretStr("xai-not-a-real-key"))

    snap = _scan_snapshot(None, None)

    assert snap["grok_scanner_active"] is True
