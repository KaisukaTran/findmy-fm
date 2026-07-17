"""P3 (docs/opus-3pct-plan.md §2): richer OPUS snapshot — the TA evidence bundle forwarded
per candidate, and a 24h %% change per open position."""

from __future__ import annotations

from datetime import datetime

import pytest

from app import market
from app.data import providers as data_providers
from app.models import Candidate, ScanRun
from app.orchestrator import brain
from app.orchestrator import models as om

# --- ta_json forwarding ---------------------------------------------------


def test_candidate_ta_json_forwarded_as_dict(db):
    scan = ScanRun()
    db.add(scan)
    db.flush()
    db.add(Candidate(
        scan_id=scan.id, symbol="BTC", decision="trade",
        ta_json='{"htf": "up", "adx": 30.0}',
    ))
    db.commit()

    row = brain._candidates(db)[0]
    assert row["ta"] == {"htf": "up", "adx": 30.0}


def test_candidate_null_ta_json_omits_key(db):
    scan = ScanRun()
    db.add(scan)
    db.flush()
    db.add(Candidate(scan_id=scan.id, symbol="ETH", decision="skip"))  # ta_json left NULL
    db.commit()

    row = brain._candidates(db)[0]
    assert "ta" not in row


def test_candidate_bad_ta_json_omits_key_defensively(db):
    scan = ScanRun()
    db.add(scan)
    db.flush()
    db.add(Candidate(scan_id=scan.id, symbol="SOL", decision="trade", ta_json="{not valid json"))
    db.commit()

    row = brain._candidates(db)[0]
    assert "ta" not in row


# --- chg24h_pct on open positions -----------------------------------------


class _FakeProvider:
    def __init__(self, closes: list[float] | None = None, *, raises: bool = False):
        self._closes = closes or []
        self._raises = raises

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 25):
        if self._raises:
            raise RuntimeError("network down")
        return [{"close": c} for c in self._closes]


def _seed_open_position(db):
    pos = om.OpusPosition(
        symbol="BTC", state=om.OPUS_WATCH, qty=1.0, avg_price=90.0, entry_price=90.0,
        opened_at=datetime.utcnow(), watch_started_at=datetime.utcnow(),
    )
    db.add(pos)
    db.commit()
    return pos


def test_chg24h_pct_present_when_provider_returns_candles(db, monkeypatch):
    _seed_open_position(db)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"BTC": 100.0})
    closes = [100.0] * 24 + [110.0]  # 25 candles: first=100, last=110 -> +10%
    monkeypatch.setattr(data_providers, "data_provider", lambda: _FakeProvider(closes))

    snap = brain.build_snapshot(db)
    row = snap["open_positions"][0]
    assert row["chg24h_pct"] == pytest.approx(10.0)


def test_chg24h_pct_omitted_on_provider_error(db, monkeypatch):
    _seed_open_position(db)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"BTC": 100.0})
    monkeypatch.setattr(data_providers, "data_provider", lambda: _FakeProvider(raises=True))

    snap = brain.build_snapshot(db)
    row = snap["open_positions"][0]
    assert "chg24h_pct" not in row


def test_chg24h_pct_omitted_when_history_too_thin(db, monkeypatch):
    """Fewer than 25 candles (e.g. a freshly-listed pair) -> omit rather than guess."""
    _seed_open_position(db)
    monkeypatch.setattr(market, "get_current_prices", lambda syms: {"BTC": 100.0})
    monkeypatch.setattr(data_providers, "data_provider", lambda: _FakeProvider([100.0] * 5))

    snap = brain.build_snapshot(db)
    row = snap["open_positions"][0]
    assert "chg24h_pct" not in row
