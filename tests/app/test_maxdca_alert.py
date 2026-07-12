"""Telegram max-DCA alert + 1-click 'add a wave' button (Kai's request 2026-07-12).

When a KSS session's DCA ladder is FULL (no auto rung left), push a Telegram alert with an
inline button. One click -> queue_manual_extra_wave -> the standard next ladder rung is added
(the ladder is extended by one) through the normal approval flow.

No network: _telegram_send / relay / service internals are stubbed.
"""

from __future__ import annotations

import pytest

from app import notify
from app.config import settings
from app.kss import service
from app.models import SESSION_ACTIVE, KssSession


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "telegram_notify_maxdca", True)
    monkeypatch.setattr(settings, "live_trading", False)   # instance_name() -> 'paper'
    monkeypatch.setattr(settings, "telegram_chat_id", "777")
    monkeypatch.setattr(notify, "enabled", lambda: True)


def _capture_send(monkeypatch):
    calls: list = []
    monkeypatch.setattr(notify, "_telegram_send",
                        lambda text, reply_markup=None: calls.append((text, reply_markup)) or True)
    return calls


_SNAP = {"symbol": "AAA", "waves": "6/6", "avg": 100.0, "market": 95.0, "upnl_pct": -5.0,
         "upnl_usd": -50.0, "deployed": 1000.0, "sl_floor": 92.0, "room_to_sl_pct": 3.3,
         "next_wave": 6, "add_price": 93.5, "add_qty": 7.0, "add_cost": 654.5, "below_sl": False}


def _stub_snapshot(monkeypatch, **over):
    snap = {**_SNAP, **over}
    monkeypatch.setattr(service, "dca_alert_snapshot", lambda db, sid: snap)
    return snap


def _session(db, **kw):
    d = {"symbol": "AAA", "entry_price": 100.0, "distance_pct": 1.5, "max_waves": 6,
         "isolated_fund": 1000.0, "tp_pct": 4.0, "timeout_x_min": 43200.0, "gap_y_min": 0.0,
         "status": SESSION_ACTIVE, "current_wave": 5, "avg_price": 100.0,
         "total_filled_qty": 10.0, "total_cost": 1000.0, "sl_pct": 8.0}
    d.update(kw)
    s = KssSession(**d)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ---- send() inline keyboard ----

def test_send_attaches_inline_keyboard(monkeypatch):
    calls = _capture_send(monkeypatch)
    ok = notify.send("hi", buttons=[[{"text": "X", "callback_data": "dca:paper:1"}]])
    assert ok
    _text, rm = calls[-1]
    assert rm == {"inline_keyboard": [[{"text": "X", "callback_data": "dca:paper:1"}]]}


def test_send_without_buttons_has_no_markup(monkeypatch):
    calls = _capture_send(monkeypatch)
    notify.send("hi")
    assert calls[-1][1] is None


# ---- alert_max_dca detection / dedup ----

def test_alert_fires_once_on_full_ladder(db, monkeypatch):
    calls = _capture_send(monkeypatch)
    _stub_snapshot(monkeypatch)
    s = _session(db, current_wave=5, max_waves=6)          # 6/6 → full
    assert notify.alert_max_dca(db) == [s.id]
    text, rm = calls[-1]
    assert "AAA" in text and "uPnL" in text and "654" in text   # status + next-rung cost shown
    kb = rm["inline_keyboard"][0]
    assert kb[0]["callback_data"] == f"dca:paper:{s.id}"        # ➕ add
    assert kb[1]["callback_data"] == f"dcax:paper:{s.id}"       # ✖ bỏ qua
    # dedup: nothing on a second pass at the same ladder depth
    calls.clear()
    assert notify.alert_max_dca(db) == []
    assert calls == []


def test_no_alert_when_ladder_not_full(db, monkeypatch):
    calls = _capture_send(monkeypatch)
    _session(db, current_wave=2, max_waves=6)
    assert notify.alert_max_dca(db) == []
    assert calls == []


def test_realert_after_ladder_extended(db, monkeypatch):
    _capture_send(monkeypatch)
    _stub_snapshot(monkeypatch)
    s = _session(db, current_wave=5, max_waves=6)
    assert notify.alert_max_dca(db) == [s.id]                # alerted at max_waves=6
    s.max_waves = 7                                          # button bumped it, then it filled full again
    s.current_wave = 6
    db.commit()
    assert notify.alert_max_dca(db) == [s.id]                # different depth → re-alert


def test_declined_session_is_muted(db, monkeypatch):
    from app import runtime
    calls = _capture_send(monkeypatch)
    _stub_snapshot(monkeypatch)
    s = _session(db, current_wave=5, max_waves=6)
    runtime.set(db, f"maxdca_declined:{s.id}", "1")          # user pressed ✖ Bỏ qua earlier
    assert notify.alert_max_dca(db) == []
    assert calls == []


def test_snapshot_previews_next_rung(db, monkeypatch):
    from app import market
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": 95.0})
    s = _session(db, current_wave=5, max_waves=6, avg_price=100.0, entry_price=100.0,
                 distance_pct=1.5, sl_pct=8.0, total_filled_qty=10.0, total_cost=1000.0)
    snap = service.dca_alert_snapshot(db, s.id)
    assert snap["symbol"] == "AAA" and snap["next_wave"] == 6
    assert snap["market"] == 95.0 and snap["upnl_pct"] == pytest.approx(-5.0)
    assert snap["add_cost"] > 0 and snap["below_sl"] is False   # 93.6 rung > 92 SL floor


def test_kill_switch_suppresses_alert(db, monkeypatch):
    calls = _capture_send(monkeypatch)
    monkeypatch.setattr(settings, "telegram_notify_maxdca", False)
    _session(db, current_wave=5, max_waves=6)
    assert notify.alert_max_dca(db) == []
    assert calls == []


# ---- queue_manual_extra_wave (service) ----

def test_extra_wave_bumps_ladder_then_delegates(db, monkeypatch):
    s = _session(db, current_wave=5, max_waves=6)
    seen = {}

    def fake_qnw(dbx, sid, amount_usd=None):
        seen["max_waves"] = db.get(KssSession, sid).max_waves
        seen["amount_usd"] = amount_usd
        return {"wave_num": 6, "price": 1.0, "quantity": 1.0, "cost": 1.0, "pending_order_id": 1}

    monkeypatch.setattr(service, "queue_next_wave", fake_qnw)
    res = service.queue_manual_extra_wave(db, s.id)
    assert seen["max_waves"] == 7            # ladder extended by one BEFORE delegating
    assert seen["amount_usd"] is None        # standard ladder-size rung (not a custom USD)
    assert res["symbol"] == "AAA" and res["wave_num"] == 6


def test_extra_wave_rejects_inactive(db):
    from app.models import SESSION_STOPPED
    s = _session(db, status=SESSION_STOPPED)
    with pytest.raises(ValueError):
        service.queue_manual_extra_wave(db, s.id)


def test_extra_wave_rolls_back_bump_on_failure(db, monkeypatch):
    s = _session(db, current_wave=5, max_waves=6)

    def boom(dbx, sid, amount_usd=None):
        raise ValueError("dưới SL")

    monkeypatch.setattr(service, "queue_next_wave", boom)
    with pytest.raises(ValueError):
        service.queue_manual_extra_wave(db, s.id)
    db.expire_all()
    assert db.get(KssSession, s.id).max_waves == 6          # bump rolled back, not persisted


# ---- /dca_add command + callback dispatch ----

def test_dca_add_command_formats_reply(db, monkeypatch):
    monkeypatch.setattr(service, "queue_manual_extra_wave",
                        lambda dbx, sid: {"symbol": "AAA", "wave_num": 6, "price": 0.5, "cost": 12.3})
    reply = notify.handle_command("/dca_add 42")
    assert "AAA" in reply and "6" in reply


def test_dca_add_command_bad_arg(db):
    reply = notify.handle_command("/dca_add abc")
    assert "session_id" in reply.lower() or "dùng" in reply.lower()


def test_callback_local_instance_dispatches_and_edits(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify, "handle_command", lambda t: seen.setdefault("cmd", t) or "✅ ok")
    monkeypatch.setattr(notify, "_answer_callback", lambda *a, **k: seen.__setitem__("answered", True))
    monkeypatch.setattr(notify, "_edit_message",
                        lambda *a, **k: seen.__setitem__("edited", (a, k)))
    cb = {"id": "cb1", "data": "dca:paper:42", "message": {"message_id": 9, "chat": {"id": 777}}}
    notify._handle_callback(cb)
    assert seen["cmd"] == "/dca_add 42"
    assert seen["answered"] is True
    assert "edited" in seen


def test_callback_rejects_unknown_chat(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(notify, "handle_command", lambda t: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(notify, "_answer_callback", lambda *a, **k: None)
    cb = {"id": "cb1", "data": "dca:paper:42", "message": {"message_id": 9, "chat": {"id": 999}}}
    notify._handle_callback(cb)
    assert called["n"] == 0                                  # unauthorized chat → not dispatched


def test_callback_sibling_instance_relays(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify, "_proxy_command",
                        lambda target, cmd: seen.setdefault("relay", (target, cmd)) or "✅ relayed")
    monkeypatch.setattr(notify, "handle_command", lambda t: seen.setdefault("local", t))
    monkeypatch.setattr(notify, "_answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(notify, "_edit_message", lambda *a, **k: None)
    cb = {"id": "cb1", "data": "dca:live:42", "message": {"message_id": 9, "chat": {"id": 777}}}
    notify._handle_callback(cb)
    assert seen["relay"] == ("live", "/dca_add 42")
    assert "local" not in seen                               # sibling session not handled locally


def test_callback_decline_routes_to_skip(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify, "handle_command", lambda t: seen.setdefault("cmd", t) or "✖ ok")
    monkeypatch.setattr(notify, "_answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(notify, "_edit_message", lambda *a, **k: None)
    cb = {"id": "cb1", "data": "dcax:paper:42", "message": {"message_id": 9, "chat": {"id": 777}}}
    notify._handle_callback(cb)
    assert seen["cmd"] == "/dca_skip 42"


def test_dca_skip_command_sets_mute(db):
    from app import runtime
    from app.db import SessionLocal
    s = _session(db, current_wave=5, max_waves=6)
    reply = notify.handle_command(f"/dca_skip {s.id}")
    assert "bỏ qua" in reply.lower()
    db2 = SessionLocal()
    assert runtime.get(db2, f"maxdca_declined:{s.id}") == "1"
    db2.close()


def test_dca_add_clears_mute(db, monkeypatch):
    from app import runtime
    from app.db import SessionLocal
    s = _session(db, current_wave=5, max_waves=6)
    runtime.set(db, f"maxdca_declined:{s.id}", "1")
    monkeypatch.setattr(service, "queue_manual_extra_wave",
                        lambda dbx, sid: {"symbol": "AAA", "wave_num": 6, "price": 0.5, "cost": 12.3})
    notify.handle_command(f"/dca_add {s.id}")
    db2 = SessionLocal()
    assert runtime.get(db2, f"maxdca_declined:{s.id}") == "0"
    db2.close()
