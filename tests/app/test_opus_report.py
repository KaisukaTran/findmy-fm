"""P4 accountability: daily Telegram/Discord report + rolling-7-day auto-freeze."""

from __future__ import annotations

from datetime import datetime, timedelta

from app import runtime
from app.config import settings
from app.orchestrator import loop, report, service
from app.orchestrator import models as om


def _closed_position(db, *, closed_at: datetime, realized_pnl: float = 0.0) -> None:
    db.add(om.OpusPosition(symbol="BTC", state=om.OPUS_CLOSED, realized_pnl=realized_pnl,
                            closed_at=closed_at, qty=0.0, avg_price=0.0))
    db.commit()


# --- maybe_daily_report ------------------------------------------------------------


def test_daily_report_first_ever_call_seeds_key_without_sending(db, monkeypatch):
    sent = []
    monkeypatch.setattr(report.notify, "send", lambda text, **kw: sent.append(text) or True)
    assert runtime.get(db, "opus_last_daily_report_date") is None

    out = report.maybe_daily_report(db)
    assert out is False
    assert sent == []
    assert runtime.get(db, "opus_last_daily_report_date") == datetime.utcnow().date().isoformat()


def test_daily_report_same_day_second_call_is_noop(db, monkeypatch):
    sent = []
    monkeypatch.setattr(report.notify, "send", lambda text, **kw: sent.append(text) or True)
    runtime.set(db, "opus_last_daily_report_date", datetime.utcnow().date().isoformat())

    out = report.maybe_daily_report(db)
    assert out is False
    assert sent == []


def test_daily_report_sends_once_for_yesterdays_activity(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_allocation_usd", 1000.0)
    monkeypatch.setattr(settings, "opus_kpi_target_pct", 3.0)
    sent = []
    monkeypatch.setattr(report.notify, "send", lambda text, **kw: sent.append(text) or True)

    today = datetime.utcnow().date()
    yesterday_hour = datetime(today.year, today.month, today.day) - timedelta(hours=10)
    db.add(om.OpusMetricHourly(hour_ts=yesterday_hour, gross_pnl=50.0, opus_cost_billed=10.0,
                                net_pnl=40.0, invested_capital=1000.0, net_pct=4.0,
                                trades=2, win_trades=1))
    db.commit()
    # stored date = yesterday -> a new day has begun, report is due
    runtime.set(db, "opus_last_daily_report_date", (today - timedelta(days=1)).isoformat())

    out = report.maybe_daily_report(db)
    assert out is True
    assert len(sent) == 1
    text = sent[0]
    assert "net $+40.00" in text
    assert "gross $+50.00" in text
    assert "phí API $10.00" in text
    assert "4.0×" in text  # net/cost coverage = 40/10 = 4.0x
    assert "lệnh đóng: 2" in text
    assert "thắng: 1" in text
    assert runtime.get(db, "opus_last_daily_report_date") == today.isoformat()


def test_daily_report_zero_yesterday_sends_nothing_but_bumps_key(db, monkeypatch):
    sent = []
    monkeypatch.setattr(report.notify, "send", lambda text, **kw: sent.append(text) or True)
    today = datetime.utcnow().date()
    runtime.set(db, "opus_last_daily_report_date", (today - timedelta(days=1)).isoformat())

    out = report.maybe_daily_report(db)
    assert out is False
    assert sent == []
    assert runtime.get(db, "opus_last_daily_report_date") == today.isoformat()


# --- maybe_auto_freeze --------------------------------------------------------------


def test_auto_freeze_fires_on_negative_window_net_with_enough_closes(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "opus_auto_freeze_enabled", True)
    monkeypatch.setattr(settings, "opus_freeze_window_days", 7)
    monkeypatch.setattr(settings, "opus_freeze_min_closed", 5)
    sent = []
    monkeypatch.setattr(report.notify, "send", lambda text, **kw: sent.append(text) or True)

    now = datetime.utcnow()
    db.add(om.OpusMetricHourly(hour_ts=now - timedelta(hours=2), net_pnl=-30.0))
    db.commit()
    for _ in range(5):
        _closed_position(db, closed_at=now - timedelta(hours=1))

    out = report.maybe_auto_freeze(db)
    assert out is True
    assert settings.opus_mode is False
    assert runtime.get(db, "opus_mode") in ("0", "False", "false")
    assert len(sent) == 1
    assert "TỰ ĐÓNG BĂNG" in sent[0]

    from app.models import AuditLog
    row = (
        db.query(AuditLog)
        .filter(AuditLog.actor == "opus", AuditLog.action == "auto_freeze")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None


def test_auto_freeze_does_not_fire_on_positive_net(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "opus_auto_freeze_enabled", True)
    monkeypatch.setattr(settings, "opus_freeze_min_closed", 5)
    sent = []
    monkeypatch.setattr(report.notify, "send", lambda text, **kw: sent.append(text) or True)

    now = datetime.utcnow()
    db.add(om.OpusMetricHourly(hour_ts=now - timedelta(hours=2), net_pnl=30.0))
    db.commit()
    for _ in range(5):
        _closed_position(db, closed_at=now - timedelta(hours=1))

    out = report.maybe_auto_freeze(db)
    assert out is False
    assert settings.opus_mode is True
    assert sent == []


def test_auto_freeze_does_not_fire_with_too_few_closes(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "opus_auto_freeze_enabled", True)
    monkeypatch.setattr(settings, "opus_freeze_min_closed", 5)
    sent = []
    monkeypatch.setattr(report.notify, "send", lambda text, **kw: sent.append(text) or True)

    now = datetime.utcnow()
    db.add(om.OpusMetricHourly(hour_ts=now - timedelta(hours=2), net_pnl=-30.0))
    db.commit()
    for _ in range(2):  # below opus_freeze_min_closed
        _closed_position(db, closed_at=now - timedelta(hours=1))

    out = report.maybe_auto_freeze(db)
    assert out is False
    assert settings.opus_mode is True
    assert sent == []


def test_auto_freeze_knob_off_is_noop(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "opus_auto_freeze_enabled", False)
    sent = []
    monkeypatch.setattr(report.notify, "send", lambda text, **kw: sent.append(text) or True)

    now = datetime.utcnow()
    db.add(om.OpusMetricHourly(hour_ts=now - timedelta(hours=2), net_pnl=-30.0))
    db.commit()
    for _ in range(5):
        _closed_position(db, closed_at=now - timedelta(hours=1))

    out = report.maybe_auto_freeze(db)
    assert out is False
    assert sent == []


def test_auto_freeze_evaluated_once_per_utc_day(db, monkeypatch):
    """Even when the losing conditions persist, a second same-day call must not re-fire
    (no double notify / no repeated audit row / mode stays whatever the first call left it)."""
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "opus_auto_freeze_enabled", True)
    monkeypatch.setattr(settings, "opus_freeze_min_closed", 5)
    sent = []
    monkeypatch.setattr(report.notify, "send", lambda text, **kw: sent.append(text) or True)

    now = datetime.utcnow()
    db.add(om.OpusMetricHourly(hour_ts=now - timedelta(hours=2), net_pnl=-30.0))
    db.commit()
    for _ in range(5):
        _closed_position(db, closed_at=now - timedelta(hours=1))

    first = report.maybe_auto_freeze(db)
    assert first is True
    assert len(sent) == 1

    # Re-enable opus_mode to prove the throttle (not the mode gate) blocks the second call.
    monkeypatch.setattr(settings, "opus_mode", True)
    second = report.maybe_auto_freeze(db)
    assert second is False
    assert len(sent) == 1  # no double notify


# --- loop.tick wiring ----------------------------------------------------------------


def test_tick_calls_report_functions_after_watch_even_when_cost_capped(db, monkeypatch):
    from app.orchestrator import watch

    calls: list[str] = []
    monkeypatch.setattr(settings, "opus_mode", True)
    monkeypatch.setattr(settings, "opus_daily_cost_cap_usd", 5.0)
    db.add(om.OpusCostLedger(billed_cost=6.0))  # over the cap
    db.commit()

    monkeypatch.setattr(watch, "run", lambda _db: calls.append("watch") or {"rides": 0})
    monkeypatch.setattr(report, "maybe_daily_report", lambda _db: calls.append("daily_report") or False)
    monkeypatch.setattr(report, "maybe_auto_freeze", lambda _db: calls.append("auto_freeze") or False)

    out = loop.tick(db)
    assert out["skipped"] == "cost_cap"  # the tick still hit the cost cap as before
    assert calls == ["watch", "daily_report", "auto_freeze"]  # order preserved, ran despite the cap


# --- new knobs round-trip -------------------------------------------------------------


def test_kss_settings_body_accepts_accountability_knobs():
    from app.routes import KssSettingsBody

    dumped = KssSettingsBody(
        opus_auto_freeze_enabled=False, opus_freeze_window_days=10, opus_freeze_min_closed=3,
    ).model_dump(exclude_none=True)
    for k in ("opus_auto_freeze_enabled", "opus_freeze_window_days", "opus_freeze_min_closed"):
        assert k in dumped, f"{k} dropped by KssSettingsBody"
    assert dumped["opus_freeze_window_days"] == 10
    assert dumped["opus_freeze_min_closed"] == 3


def test_accountability_knobs_persist_and_restore(db):
    runtime.set_kss_settings(db, {
        "opus_auto_freeze_enabled": False, "opus_freeze_window_days": 10, "opus_freeze_min_closed": 3,
    })
    assert not settings.opus_auto_freeze_enabled
    assert settings.opus_freeze_window_days == 10
    assert settings.opus_freeze_min_closed == 3

    # simulate a fresh process boot at the defaults, then restore from runtime_config
    settings.opus_auto_freeze_enabled = True
    settings.opus_freeze_window_days = 7
    settings.opus_freeze_min_closed = 5
    runtime.sync_from_db(db)
    assert not settings.opus_auto_freeze_enabled
    assert settings.opus_freeze_window_days == 10
    assert settings.opus_freeze_min_closed == 3


def test_state_carries_auto_freeze_enabled_flag(db, monkeypatch):
    monkeypatch.setattr(settings, "opus_auto_freeze_enabled", False)
    assert service.state(db)["auto_freeze_enabled"] is False
