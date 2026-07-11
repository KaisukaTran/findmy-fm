"""UTC clock helper — a non-deprecated drop-in for the removed ``datetime.utcnow()``.

Returns a NAIVE UTC datetime (tzinfo stripped) so it is byte-for-byte equivalent to the old
``datetime.utcnow()``: every stored timestamp and every naive-vs-naive comparison keeps working
unchanged. We deliberately do NOT switch to tz-aware datetimes here — the DB columns hold naive
UTC values, and mixing aware/naive would raise ``TypeError`` on comparison.

Leaf module (stdlib only) so any layer, including ``app.models`` and ``app.db``, can import it
without a circular dependency.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (same value as the deprecated ``datetime.utcnow()``)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
