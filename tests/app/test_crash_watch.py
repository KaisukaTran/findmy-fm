"""The market-wide crash watch alerts, and never does anything else.

The halt-buying half of this feature was measured and dropped (see `app/crash_watch.py`), so the
load-bearing property here is a negative one: this module must never touch an order. The last
test in this file is the one that matters most.
"""
from __future__ import annotations

import pytest

from app import crash_watch, runtime
from app.config import settings


def _bars(prev_high: float, low: float) -> list[dict]:
    """Two bars: the previous one's high, and the latest one's low."""
    return [{"high": prev_high, "low": prev_high * 0.99, "close": prev_high},
            {"high": low, "low": low, "close": low}]


def _universe(n_falling: int, n_calm: int, drop_pct: float = 25.0) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for i in range(n_falling):
        out[f"FALL{i}"] = _bars(100.0, 100.0 * (1 - drop_pct / 100.0))
    for i in range(n_calm):
        out[f"CALM{i}"] = _bars(100.0, 99.0)
    return out


def _on(monkeypatch, **over):
    monkeypatch.setattr(settings, "crash_alert_enabled", True)
    monkeypatch.setattr(settings, "crash_alert_drop_pct", 20.0)
    monkeypatch.setattr(settings, "crash_alert_breadth_pct", 60.0)
    monkeypatch.setattr(settings, "crash_alert_min_symbols", 30)
    monkeypatch.setattr(settings, "crash_alert_cooldown_min", 180.0)
    for k, v in over.items():
        monkeypatch.setattr(settings, k, v)


# --------------------------------------------------------------------------- breadth maths


def test_breadth_counts_the_fall_against_the_previous_bars_high():
    pct, hits, measured = crash_watch.breadth(_universe(3, 1), 20.0)
    assert (hits, measured) == (3, 4)
    assert pct == pytest.approx(75.0)


def test_a_symbol_that_cannot_be_measured_is_skipped_not_counted_as_calm():
    """The trap: diluting breadth with unmeasurable symbols turns a real crash into silence."""
    uni = _universe(3, 0)
    uni["NODATA"] = [{"high": 0.0, "low": 0.0, "close": 0.0}]   # zero high
    uni["ONEBAR"] = [{"high": 10.0, "low": 9.0, "close": 9.0}]  # no previous bar
    pct, hits, measured = crash_watch.breadth(uni, 20.0)
    assert (hits, measured) == (3, 3)
    assert pct == pytest.approx(100.0)  # not 60% — the two unmeasurable symbols are excluded


def test_a_fall_exactly_on_the_threshold_counts():
    pct, hits, _ = crash_watch.breadth(_universe(1, 0, drop_pct=20.0), 20.0)
    assert hits == 1 and pct == pytest.approx(100.0)


def test_empty_universe_is_zero_not_a_crash():
    assert crash_watch.breadth({}, 20.0) == (0.0, 0, 0)


# --------------------------------------------------------------------------- the alert gate


def test_disabled_returns_none_and_never_reads_the_universe(db, monkeypatch):
    monkeypatch.setattr(settings, "crash_alert_enabled", False)
    assert crash_watch.evaluate(db, _universe(40, 0)) is None


def test_below_the_breadth_threshold_reports_but_does_not_alert(db, monkeypatch):
    _on(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(crash_watch.notify, "event", lambda k, t, **kw: sent.append(t))
    r = crash_watch.evaluate(db, _universe(20, 20))  # 50% < 60%
    assert r is not None and r["breadth_pct"] == pytest.approx(50.0)
    assert "alerted" not in r
    assert sent == []


def test_a_broad_fall_alerts(db, monkeypatch):
    _on(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(crash_watch.notify, "event", lambda k, t, **kw: sent.append((k, t)))
    r = crash_watch.evaluate(db, _universe(35, 5))  # 87.5% >= 60%
    assert r["alerted"] is True
    assert len(sent) == 1
    kind, text = sent[0]
    assert kind == "risk"          # bypasses the Telegram master mute
    assert "35/40" in text
    assert "KHÔNG tự dừng mua" in text  # the message must not imply the bot stopped trading


def test_too_few_symbols_stays_silent(db, monkeypatch):
    """Sample-size guard: the session-depth version of this rule died of exactly this."""
    _on(monkeypatch, crash_alert_min_symbols=30)
    sent: list = []
    monkeypatch.setattr(crash_watch.notify, "event", lambda k, t, **kw: sent.append(t))
    assert crash_watch.evaluate(db, _universe(10, 0)) is None  # 100% breadth, but only 10 symbols
    assert sent == []


def test_the_cooldown_throttles_a_second_alert(db, monkeypatch):
    _on(monkeypatch)
    sent: list = []
    monkeypatch.setattr(crash_watch.notify, "event", lambda k, t, **kw: sent.append(t))
    uni = _universe(35, 5)
    assert crash_watch.evaluate(db, uni)["alerted"] is True
    second = crash_watch.evaluate(db, uni)
    assert second.get("throttled") is True and "alerted" not in second
    assert len(sent) == 1


def test_an_unreadable_timestamp_alerts_rather_than_staying_silent(db, monkeypatch):
    """Fail loud, not quiet: a corrupt stamp must not mute a crash alert forever."""
    _on(monkeypatch)
    runtime.set(db, crash_watch.KEY_LAST_ALERT, "not-a-timestamp")
    sent: list = []
    monkeypatch.setattr(crash_watch.notify, "event", lambda k, t, **kw: sent.append(t))
    assert crash_watch.evaluate(db, _universe(35, 5))["alerted"] is True
    assert len(sent) == 1


# --------------------------------------------------------------------------- the invariant


def test_the_module_never_touches_an_order():
    """Alert only. If this module ever imports the order path, that is the bug this catches."""
    import pathlib

    src = pathlib.Path(crash_watch.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[-1]  # skip the module docstring, which discusses orders by name
    for forbidden in ("orders.", "queue_order", "approve_order", "PendingOrder",
                      "KssSession", "freeze(", "runtime.freeze"):
        assert forbidden not in body, f"crash_watch must not reference {forbidden}"
