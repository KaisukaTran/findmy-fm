"""
Display-time helpers — convert stored UTC timestamps to the configured local zone.

Storage stays naive-UTC everywhere (datetime.utcnow); these only shift values for the
dashboard/charts so users see Vietnam time (UTC+`tz_offset_hours`). Never use these for
logic/comparisons.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.config import settings


def _parse(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def to_local(value: str | datetime | None) -> datetime | None:
    """Shift a naive-UTC timestamp (ISO string or datetime) to the display zone."""
    dt = _parse(value)
    if dt is None:
        return None
    return dt + timedelta(hours=settings.tz_offset_hours)


def local_hms(value: str | datetime | None) -> str:
    """`HH:MM:SS` in the display zone (empty string if unparseable)."""
    dt = to_local(value)
    return dt.strftime("%H:%M:%S") if dt else ""


def local_dt(value: str | datetime | None) -> str:
    """`YYYY-MM-DD HH:MM:SS` in the display zone."""
    dt = to_local(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""
