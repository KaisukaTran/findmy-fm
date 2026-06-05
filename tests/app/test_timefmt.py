"""Display timezone: UTC stored timestamps shown in VN time (UTC+7)."""

from __future__ import annotations

from datetime import datetime

from app import timefmt
from app.config import settings


def test_local_hms_shifts_by_offset(monkeypatch):
    monkeypatch.setattr(settings, "tz_offset_hours", 7)
    # 2026-06-05 08:30:00 UTC -> 15:30:00 VN
    assert timefmt.local_hms("2026-06-05T08:30:00") == "15:30:00"


def test_local_dt_full(monkeypatch):
    monkeypatch.setattr(settings, "tz_offset_hours", 7)
    assert timefmt.local_dt("2026-06-05T08:30:00.123456") == "2026-06-05 15:30:00"


def test_handles_datetime_and_empty(monkeypatch):
    monkeypatch.setattr(settings, "tz_offset_hours", 7)
    assert timefmt.local_hms(datetime(2026, 6, 5, 0, 0, 0)) == "07:00:00"
    assert timefmt.local_hms("") == ""
    assert timefmt.local_hms(None) == ""
    assert timefmt.local_hms("not-a-date") == ""


def test_offset_configurable(monkeypatch):
    monkeypatch.setattr(settings, "tz_offset_hours", 0)
    assert timefmt.local_hms("2026-06-05T08:30:00") == "08:30:00"
