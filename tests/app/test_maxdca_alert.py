"""Max-DCA — PULL-based UX (Kai 2026-07-12, redesign): no proactive push. The full-ladder
situation is folded into /summary as a compact line + a '📋 Liệt kê' button; tapping it (or
/dca_list) sends one detail card per session with '➕ Thêm ~$X' / '✖ Bỏ qua' buttons. A press
maps to /dca_add or /dca_skip so it reuses command auth + the paper→live relay.

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
    from app import market
    monkeypatch.setattr(settings, "telegram_notify_maxdca", True)
    monkeypatch.setattr(settings, "maxdca_allow_add", False)         # production default = info-only
    monkeypatch.setattr(settings, "maxdca_max_underwater_pct", 8.0)
    monkeypatch.setattr(settings, "max_session_deploy_usd", 0.0)     # cap off (no headroom filter)
    monkeypatch.setattr(settings, "live_trading", False)   # instance_name() -> 'paper'
    monkeypatch.setattr(settings, "telegram_chat_id", "777")
    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(market, "get_current_prices",
                        lambda syms, force=False: {"AAA": 99.0})    # avg 100 → −1% (not deep)


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
    monkeypatch.setattr(service, "dca_alert_snapshot", lambda db, sid: {**snap, "symbol": _SNAP["symbol"]})
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
    assert notify.send("hi", buttons=[[{"text": "X", "callback_data": "dca:paper:1"}]])
    assert calls[-1][1] == {"inline_keyboard": [[{"text": "X", "callback_data": "dca:paper:1"}]]}


def test_send_without_buttons_has_no_markup(monkeypatch):
    calls = _capture_send(monkeypatch)
    notify.send("hi")
    assert calls[-1][1] is None


# ---- full-ladder set (pull surface) ----

def test_full_sessions_excludes_not_full_and_declined(db):
    from app import runtime
    a = _session(db, current_wave=5, max_waves=6)          # full
    _session(db, current_wave=2, max_waves=6)              # not full → excluded
    d = _session(db, current_wave=5, max_waves=6)          # full but muted → excluded
    runtime.set(db, f"maxdca_declined:{d.id}", "1")
    ids = {r.id for r in notify._maxdca_full_sessions(db)}
    assert ids == {a.id}


# ---- /summary fold-in + list button (NO proactive push) ----

def test_summary_line_shows_count(db):
    _session(db, current_wave=5, max_waves=6)
    line = notify.maxdca_summary_line(db)
    assert "cạn nấc" in line and "AAA" in line and "Liệt kê" in line


def test_summary_line_empty_when_none(db):
    _session(db, current_wave=2, max_waves=6)              # not full
    assert notify.maxdca_summary_line(db) == ""


def test_summary_line_off_when_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "telegram_notify_maxdca", False)
    _session(db, current_wave=5, max_waves=6)
    assert notify.maxdca_summary_line(db) == ""


def test_list_button_present_then_none(db):
    s = _session(db, current_wave=5, max_waves=6)
    btn = notify.maxdca_list_button(db)
    assert btn and btn[0][0]["callback_data"] == "dcalist:paper"
    s.current_wave = 2                                     # no longer full
    db.commit()
    assert notify.maxdca_list_button(db) is None


def test_reply_buttons_only_for_summary(db):
    _session(db, current_wave=5, max_waves=6)
    assert notify._reply_buttons("/summary") is not None
    assert notify._reply_buttons("/status") is None


# ---- send_maxdca_list (on-demand detail cards) ----

def test_send_list_cards_info_only_by_default(db, monkeypatch):
    """maxdca_allow_add off (default) → card shows status + only the '✖ Bỏ qua' button (no ➕)."""
    calls = _capture_send(monkeypatch)
    _stub_snapshot(monkeypatch)
    s = _session(db, current_wave=5, max_waves=6)
    assert notify.send_maxdca_list(db) == 1
    text, rm = calls[-1]
    kb = rm["inline_keyboard"][0]
    assert "AAA" in text
    assert len(kb) == 1 and kb[0]["callback_data"] == f"dcax:paper:{s.id}"   # only mute


def test_send_list_cards_add_button_when_enabled(db, monkeypatch):
    monkeypatch.setattr(settings, "maxdca_allow_add", True)
    calls = _capture_send(monkeypatch)
    _stub_snapshot(monkeypatch)
    s = _session(db, current_wave=5, max_waves=6)
    notify.send_maxdca_list(db)
    kb = calls[-1][1]["inline_keyboard"][0]
    assert kb[0]["callback_data"] == f"dca:paper:{s.id}"     # ➕ add
    assert kb[1]["callback_data"] == f"dcax:paper:{s.id}"    # ✖ mute


def test_send_list_excludes_declined(db, monkeypatch):
    from app import runtime
    calls = _capture_send(monkeypatch)
    _stub_snapshot(monkeypatch)
    s = _session(db, current_wave=5, max_waves=6)
    runtime.set(db, f"maxdca_declined:{s.id}", "1")
    assert notify.send_maxdca_list(db) == 0
    assert calls == []


def test_list_excludes_too_deep_underwater(db, monkeypatch):
    from app import market
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": 90.0})  # −10%
    _session(db, current_wave=5, max_waves=6, avg_price=100.0)   # deeper than 8% → excluded
    assert notify._maxdca_full_sessions(db) == []


def test_list_excludes_no_deploy_headroom(db, monkeypatch):
    monkeypatch.setattr(settings, "max_session_deploy_usd", 100.0)
    _session(db, current_wave=5, max_waves=6, total_cost=100.0)  # deployed == cap → 0 headroom → excluded
    assert notify._maxdca_full_sessions(db) == []


# ---- service: preview + add ----

def test_snapshot_previews_next_rung(db, monkeypatch):
    from app import market
    monkeypatch.setattr(market, "get_current_prices", lambda syms, force=False: {"AAA": 95.0})
    s = _session(db, current_wave=5, max_waves=6, avg_price=100.0, entry_price=100.0,
                 distance_pct=1.5, sl_pct=8.0, total_filled_qty=10.0, total_cost=1000.0)
    snap = service.dca_alert_snapshot(db, s.id)
    assert snap["symbol"] == "AAA" and snap["next_wave"] == 6
    assert snap["market"] == 95.0 and snap["upnl_pct"] == pytest.approx(-5.0)
    assert snap["add_cost"] > 0 and snap["below_sl"] is False


def test_extra_wave_bumps_ladder_then_delegates(db, monkeypatch):
    s = _session(db, current_wave=5, max_waves=6)
    seen = {}

    def fake_qnw(dbx, sid, amount_usd=None):
        seen["max_waves"] = db.get(KssSession, sid).max_waves
        seen["amount_usd"] = amount_usd
        return {"wave_num": 6, "price": 1.0, "quantity": 1.0, "cost": 1.0, "pending_order_id": 1}

    monkeypatch.setattr(service, "queue_next_wave", fake_qnw)
    res = service.queue_manual_extra_wave(db, s.id)
    assert seen["max_waves"] == 7 and seen["amount_usd"] is None
    assert res["symbol"] == "AAA" and res["wave_num"] == 6


def test_extra_wave_rejects_inactive(db):
    from app.models import SESSION_STOPPED
    s = _session(db, status=SESSION_STOPPED)
    with pytest.raises(ValueError):
        service.queue_manual_extra_wave(db, s.id)


def test_extra_wave_rolls_back_bump_on_failure(db, monkeypatch):
    s = _session(db, current_wave=5, max_waves=6)
    monkeypatch.setattr(service, "queue_next_wave",
                        lambda dbx, sid, amount_usd=None: (_ for _ in ()).throw(ValueError("dưới SL")))
    with pytest.raises(ValueError):
        service.queue_manual_extra_wave(db, s.id)
    db.expire_all()
    assert db.get(KssSession, s.id).max_waves == 6


# ---- commands ----

def test_dca_add_command_formats_reply(db, monkeypatch):
    monkeypatch.setattr(service, "queue_manual_extra_wave",
                        lambda dbx, sid: {"symbol": "AAA", "wave_num": 6, "price": 0.5, "cost": 12.3})
    reply = notify.handle_command("/dca_add 42")
    assert "AAA" in reply and "6" in reply


def test_dca_add_command_bad_arg(db):
    assert "session_id" in notify.handle_command("/dca_add abc").lower()


def test_dca_list_command_sends(db, monkeypatch):
    _capture_send(monkeypatch)
    _stub_snapshot(monkeypatch)
    _session(db, current_wave=5, max_waves=6)
    reply = notify.handle_command("/dca_list")
    assert "1" in reply


def test_dca_skip_command_sets_mute(db):
    from app import runtime
    from app.db import SessionLocal
    s = _session(db, current_wave=5, max_waves=6)
    assert "bỏ qua" in notify.handle_command(f"/dca_skip {s.id}").lower()
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


# ---- callback dispatch ----

def test_callback_add_dispatches_and_edits(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify, "handle_command", lambda t: seen.setdefault("cmd", t) or "✅ ok")
    monkeypatch.setattr(notify, "_answer_callback", lambda *a, **k: seen.__setitem__("ans", True))
    monkeypatch.setattr(notify, "_edit_message", lambda *a, **k: seen.__setitem__("edit", a))
    notify._handle_callback({"id": "c1", "data": "dca:paper:42",
                             "message": {"message_id": 9, "chat": {"id": 777}}})
    assert seen["cmd"] == "/dca_add 42" and seen["ans"] and "edit" in seen


def test_callback_decline_routes_to_skip(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify, "handle_command", lambda t: seen.setdefault("cmd", t) or "✖ ok")
    monkeypatch.setattr(notify, "_answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(notify, "_edit_message", lambda *a, **k: None)
    notify._handle_callback({"id": "c1", "data": "dcax:paper:42",
                             "message": {"message_id": 9, "chat": {"id": 777}}})
    assert seen["cmd"] == "/dca_skip 42"


def test_callback_dcalist_sends_list(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify, "handle_command", lambda t: seen.setdefault("cmd", t) or "📋 sent")
    monkeypatch.setattr(notify, "_answer_callback", lambda *a, **k: None)
    notify._handle_callback({"id": "c1", "data": "dcalist:paper",
                             "message": {"message_id": 9, "chat": {"id": 777}}})
    assert seen["cmd"] == "/dca_list"


def test_callback_rejects_unknown_chat(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(notify, "handle_command", lambda t: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(notify, "_answer_callback", lambda *a, **k: None)
    notify._handle_callback({"id": "c1", "data": "dca:paper:42",
                             "message": {"message_id": 9, "chat": {"id": 999}}})
    assert called["n"] == 0


def test_callback_sibling_relays(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify, "_proxy_command",
                        lambda target, cmd: seen.setdefault("relay", (target, cmd)) or "✅ relayed")
    monkeypatch.setattr(notify, "handle_command", lambda t: seen.setdefault("local", t))
    monkeypatch.setattr(notify, "_answer_callback", lambda *a, **k: None)
    monkeypatch.setattr(notify, "_edit_message", lambda *a, **k: None)
    notify._handle_callback({"id": "c1", "data": "dca:live:42",
                             "message": {"message_id": 9, "chat": {"id": 777}}})
    assert seen["relay"] == ("live", "/dca_add 42") and "local" not in seen
