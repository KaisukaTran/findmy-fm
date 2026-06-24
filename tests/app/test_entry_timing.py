"""Entry-timing gate (_falling_knife_veto): don't open a DCA ladder into a coin whose
short-term momentum is falling (Supertrend down AND MACD histogram negative).

Tighter than _downtrend_veto (which needs HTF+ST+ADX all confirming) — it catches the mild
short-term drops that left freshly-opened sessions sitting red. Toggle: entry_momentum_gate."""

from __future__ import annotations

from app import scanner
from app.config import settings


def _ta(st, macd_h, htf="up", adx=10.0):
    return {"st": st, "macd_h": macd_h, "htf": htf, "adx": adx}


def test_falling_knife_vetoes_st_down_macd_negative(monkeypatch):
    monkeypatch.setattr(settings, "entry_momentum_gate", True)
    assert scanner._falling_knife_veto(_ta("down", -0.5)) is not None


def test_falling_knife_allows_when_momentum_not_falling(monkeypatch):
    monkeypatch.setattr(settings, "entry_momentum_gate", True)
    assert scanner._falling_knife_veto(_ta("up", 0.5)) is None     # clear uptrend
    assert scanner._falling_knife_veto(_ta("up", -0.5)) is None    # ST up → not a knife
    assert scanner._falling_knife_veto(_ta("down", 0.3)) is None   # MACDh positive → turning up


def test_falling_knife_toggle_off(monkeypatch):
    monkeypatch.setattr(settings, "entry_momentum_gate", False)
    assert scanner._falling_knife_veto(_ta("down", -0.5)) is None


def test_entry_momentum_gate_is_runtime_tunable():
    from app.runtime import KSS_SETTING_FIELDS
    assert "entry_momentum_gate" in KSS_SETTING_FIELDS
