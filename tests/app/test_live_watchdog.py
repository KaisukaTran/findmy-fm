"""Unit tests for scripts/live_watchdog.py's decision logic (2026-09-22 split-brain outage).

The watchdog is a standalone script (runs as SYSTEM outside the app process), so it is
imported here BY PATH rather than as a package — and only its pure helpers are exercised.
`main()` is never called: no network, no subprocess, no real clock/sleep. Process-control
helpers (`kill_port_holders`, `kill_process_tree`, `start_app`) are called with fake
`runner`/`popen` callables so the shape of the command is pinned without spawning anything.
"""

from __future__ import annotations

import http.client
import importlib.util
import inspect
import subprocess
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "live_watchdog.py"
_spec = importlib.util.spec_from_file_location("live_watchdog", _SCRIPT_PATH)
watchdog = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(watchdog)


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    """Every watchdog function that logs writes to `LOG` (data/watchdog.log by default) —
    route it to a throwaway path for every test in this file so running the suite never
    pollutes the real, production watchdog log. (2026-09-23: an earlier test run already
    appended fake "PID 4242"/"PID 424242" lines to the real data/watchdog.log — left alone
    deliberately, per instruction, but no test should add to that again.)"""
    monkeypatch.setattr(watchdog, "LOG", tmp_path / "watchdog.log")
    yield


# --- health() (2026-09-23: must never raise, treat ANY failure as unreachable) -------------


def test_health_returns_none_on_incomplete_read(monkeypatch):
    """The bug: an urlopen failure narrower than Exception missed http.client.HTTPException
    (IncompleteRead / BadStatusLine on a truncated or malformed reply — e.g. mid-restart),
    which propagated out uncaught and killed the watchdog's main loop until the next reboot."""

    def _boom(*a, **kw):
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", _boom)

    assert watchdog.health() is None


def test_health_returns_none_on_bad_status_line(monkeypatch):
    def _boom(*a, **kw):
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", _boom)

    assert watchdog.health() is None


def test_health_returns_none_on_any_unexpected_exception(monkeypatch):
    """The catch is deliberately `Exception`, not a maintained list of exception types — pin
    that broader contract directly, not just via the two types that motivated it."""

    def _boom(*a, **kw):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", _boom)

    assert watchdog.health() is None


class _Resp:
    def __init__(self, raw: bytes):
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("raw", [b"[1, 2]", b'"ok"', b"42", b"{}", b'{"detail": "Not Found"}'])
def test_health_treats_non_app_json_as_unreachable(monkeypatch, raw):
    """Round-3 cross-check: a non-object body raised AttributeError in classify() every tick
    without ever counting a failure (never restarted), and a bare {} read as healthy. Only a JSON
    object carrying the app's own "status" key is the app answering."""
    monkeypatch.setattr(watchdog.urllib.request, "urlopen", lambda *a, **k: _Resp(raw))
    assert watchdog.health() is None
    assert watchdog.classify(watchdog.health()) == (False, "unreachable")


def test_health_passes_a_real_app_body_through(monkeypatch):
    body = b'{"status": "ok", "stalled": false, "should_run": true, "scheduler_running": true}'
    monkeypatch.setattr(watchdog.urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    assert watchdog.classify(watchdog.health()) == (True, "")


# --- main() structural checks (main() itself runs an unbounded real loop with real
# time.sleep/subprocess calls, so — per the module docstring — it is never invoked directly in
# this file; these pin the fix at the source level instead, the same style already used by
# tests/app/test_knob_round_trip.py for a similar "the shape of the code, not its output"
# guarantee). ------------------------------------------------------------------------------


def test_main_wraps_each_loop_iteration_so_one_bad_tick_cannot_kill_it():
    src = inspect.getsource(watchdog.main)
    assert "try:" in src and "except Exception" in src


def test_main_kills_both_the_app_port_and_the_scheduler_lock_port_before_relaunching():
    """2026-09-23 item 6: a hung process this watchdog never launched itself can hold the
    scheduler singleton lock forever — clearing only PORT before a relaunch is not enough."""
    src = inspect.getsource(watchdog.main)
    assert "kill_port_holders(PORT)" in src
    assert "kill_port_holders(SCHEDULER_LOCK_PORT)" in src


def test_start_timeout_is_300_seconds():
    """Raised from 120s: a real boot has been measured taking ~151s launch-to-bind."""
    assert watchdog.START_TIMEOUT == 300.0


def test_scheduler_lock_port_constant_matches_the_app_default():
    assert watchdog.SCHEDULER_LOCK_PORT == "8801"


# --- classify ---------------------------------------------------------------------------


def test_classify_unreachable_when_health_is_none():
    healthy, reason = watchdog.classify(None)
    assert healthy is False
    assert reason == "unreachable"


def test_classify_healthy_when_ok_and_not_stalled():
    healthy, reason = watchdog.classify({"status": "ok", "stalled": False, "stall_reason": None})
    assert healthy is True
    assert reason == ""


def test_classify_unhealthy_with_reason_when_stalled():
    healthy, reason = watchdog.classify(
        {"status": "ok", "stalled": True, "stall_reason": "scheduler_not_running"}
    )
    assert healthy is False
    assert reason == "scheduler_not_running"


def test_classify_falls_back_to_generic_reason_when_stalled_but_reason_missing():
    """An older/partial health body (or a test double) may say stalled without a reason —
    the watchdog must still classify it unhealthy, not crash or silently pass it as OK."""
    healthy, reason = watchdog.classify({"stalled": True})
    assert healthy is False
    assert reason == "stalled"


# --- is_confirmed_started ------------------------------------------------------------------


def test_is_confirmed_started_true_only_with_scheduler_running_and_not_stalled():
    assert watchdog.is_confirmed_started(
        {"scheduler_running": True, "stalled": False}
    ) is True


def test_is_confirmed_started_false_when_unreachable():
    assert watchdog.is_confirmed_started(None) is False


def test_is_confirmed_started_false_when_scheduler_not_yet_running():
    """The 2026-09-22 bug in miniature: a process can answer /health with stalled=False
    (still inside its own boot grace) while the scheduler has not started at all. A fresh
    launch must not be accepted on that basis."""
    assert watchdog.is_confirmed_started(
        {"scheduler_running": False, "stalled": False}
    ) is False


def test_is_confirmed_started_false_when_stalled_even_if_scheduler_running():
    assert watchdog.is_confirmed_started(
        {"scheduler_running": True, "stalled": True, "stall_reason": "cycle_stalled"}
    ) is False


def test_is_confirmed_started_true_when_should_run_is_false():
    """2026-09-23 round-2 cross-check: should_run: false means the OPERATOR stopped the
    scheduler in this process on purpose — waiting up to START_TIMEOUT for scheduler_running
    to become true would wait 300s for something that will never happen. HTTP answering and
    not stalled is "started" enough."""
    assert watchdog.is_confirmed_started(
        {"status": "ok", "should_run": False, "scheduler_running": False, "stalled": False}
    ) is True


def test_is_confirmed_started_false_when_should_run_false_but_stalled():
    """should_run: false does not override an ACTUAL stall (e.g. a wedged process that also
    happens to have its scheduler off) — stalled always wins."""
    assert watchdog.is_confirmed_started(
        {"should_run": False, "scheduler_running": False, "stalled": True}
    ) is False


def test_is_confirmed_started_still_requires_scheduler_running_when_key_absent():
    """A body from an app that predates the `should_run` field must keep the OLD strict
    behaviour — `.get("should_run", True)` must not silently start accepting HTTP-only as
    "started" just because the field happens to be missing."""
    assert watchdog.is_confirmed_started(
        {"scheduler_running": False, "stalled": False}
    ) is False


# --- restart budget ---------------------------------------------------------------------


def test_prune_restart_log_drops_entries_outside_the_window():
    restarts = [0.0, 100.0, 1000.0, 1799.0]
    now = 1800.0
    pruned = watchdog.prune_restart_log(restarts, now, window=1800.0)
    # now - t < window: only the entry exactly at the window's edge (t=0.0, gap=1800) drops.
    assert pruned == [100.0, 1000.0, 1799.0]


def test_restart_budget_ok_true_under_the_cap():
    restarts = [0.0, 10.0]
    assert watchdog.restart_budget_ok(restarts, now=20.0, window=1800.0, max_restarts=3) is True


def test_restart_budget_ok_false_at_the_cap():
    restarts = [0.0, 10.0, 20.0]
    assert watchdog.restart_budget_ok(restarts, now=30.0, window=1800.0, max_restarts=3) is False


def test_restart_budget_recovers_once_the_window_passes():
    restarts = [0.0, 10.0, 20.0]
    assert watchdog.restart_budget_ok(restarts, now=1801.0, window=1800.0, max_restarts=3) is True


# --- still_starting -----------------------------------------------------------------------


def test_still_starting_true_within_timeout():
    assert watchdog.still_starting(100.0, now=150.0, timeout=120.0) is True


def test_still_starting_false_after_timeout():
    assert watchdog.still_starting(100.0, now=221.0, timeout=120.0) is False


def test_still_starting_false_when_no_launch_recorded():
    assert watchdog.still_starting(None, now=100.0, timeout=120.0) is False


# --- wait_for_start (injected clock/sleep/poll — no real time or network) ------------------


def test_wait_for_start_returns_true_as_soon_as_confirmed():
    calls = {"sleeps": 0}
    responses = iter([
        {"scheduler_running": False, "stalled": False},
        {"scheduler_running": True, "stalled": False},
    ])
    clock_values = iter([0.0, 0.0, 5.0, 5.0])  # deadline calc, then two loop checks

    def fake_clock():
        return next(clock_values, 5.0)

    def fake_sleep(_secs):
        calls["sleeps"] += 1

    ok = watchdog.wait_for_start(
        poll=lambda: next(responses),
        sleep=fake_sleep,
        clock=fake_clock,
        timeout=120.0,
        interval=5.0,
    )

    assert ok is True
    assert calls["sleeps"] == 1


def test_wait_for_start_returns_false_once_timeout_elapses():
    # First call establishes the deadline (0.0 + 120.0); every call after that reports
    # "already past it", so the while-loop body never executes and the function falls
    # through to its own final poll — never spins forever on a clock that never advances.
    clock_values = iter([0.0])

    def fake_clock():
        return next(clock_values, 999.0)

    ok = watchdog.wait_for_start(
        poll=lambda: {"scheduler_running": False, "stalled": False},
        sleep=lambda _s: None,
        clock=fake_clock,
        timeout=120.0,
        interval=5.0,
    )

    assert ok is False


class _FakeExitedProc:
    """A Popen stand-in whose .poll() always reports the process already exited."""

    def __init__(self, returncode: int):
        self.returncode = returncode
        self.pid = 4242

    def poll(self):
        return self.returncode


def test_wait_for_start_fails_fast_when_the_child_has_already_exited():
    """2026-09-23 round-2 cross-check: a process that crashed on import must not be polled for
    the full START_TIMEOUT (300s) before the outer loop gets another chance to react."""
    poll_calls = {"n": 0}

    def fake_poll_health():
        poll_calls["n"] += 1
        return {"scheduler_running": False, "stalled": False}

    sleep_calls = {"n": 0}

    ok = watchdog.wait_for_start(
        poll=fake_poll_health,
        sleep=lambda _s: sleep_calls.__setitem__("n", sleep_calls["n"] + 1),
        clock=lambda: 0.0,  # never reaches the deadline on its own
        timeout=300.0,
        interval=5.0,
        child=_FakeExitedProc(3),
    )

    assert ok is False
    assert poll_calls["n"] == 0, "must not even bother polling /health for an exited process"
    assert sleep_calls["n"] == 0, "must not sleep out any part of the interval either"


def test_wait_for_start_logs_the_exit_code_of_an_early_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, "LOG", tmp_path / "watchdog.log")

    watchdog.wait_for_start(
        poll=lambda: None,
        sleep=lambda _s: None,
        clock=lambda: 0.0,
        timeout=300.0,
        interval=5.0,
        child=_FakeExitedProc(3),
    )

    logged = (tmp_path / "watchdog.log").read_text(encoding="utf-8")
    assert "exited early" in logged
    assert "code 3" in logged


def test_wait_for_start_ignores_a_child_that_is_still_alive():
    """poll() returning None means the process has NOT exited (subprocess.Popen's own
    contract) — must not be mistaken for an early exit."""

    class _AliveProc:
        def poll(self):
            return None

    ok = watchdog.wait_for_start(
        poll=lambda: {"scheduler_running": True, "stalled": False},
        sleep=lambda _s: None,
        clock=lambda: 0.0,
        timeout=300.0,
        interval=5.0,
        child=_AliveProc(),
    )

    assert ok is True


def test_wait_for_start_checks_for_an_exit_that_happens_mid_wait():
    """The child dies partway through the poll loop, not before the first check."""
    poll_count = {"n": 0}

    class _DiesOnSecondCheck:
        def __init__(self):
            self.calls = 0

        def poll(self):
            self.calls += 1
            return None if self.calls == 1 else 3

    def fake_poll_health():
        poll_count["n"] += 1
        return {"scheduler_running": False, "stalled": False}

    clock_values = iter([0.0, 0.0, 5.0])  # deadline calc, first loop check, second loop check

    ok = watchdog.wait_for_start(
        poll=fake_poll_health,
        sleep=lambda _s: None,
        clock=lambda: next(clock_values, 5.0),
        timeout=300.0,
        interval=5.0,
        child=_DiesOnSecondCheck(),
    )

    assert ok is False
    assert poll_count["n"] == 1  # health() was tried once, before the child died


# --- process control call shapes (fake runner/popen — nothing real spawned) ----------------


def test_kill_port_holders_targets_pids_listening_on_the_configured_port():
    netstat_output = (
        "  TCP    127.0.0.1:8001         0.0.0.0:0              LISTENING       4242\n"
        "  TCP    127.0.0.1:5432         0.0.0.0:0              LISTENING       9999\n"
    )
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "netstat":
            return subprocess.CompletedProcess(cmd, 0, stdout=netstat_output, stderr="")
        return subprocess.CompletedProcess(cmd, 0)

    watchdog.kill_port_holders(runner=fake_runner)

    taskkill_calls = [c for c in calls if c[0] == "taskkill"]
    assert len(taskkill_calls) == 1
    assert taskkill_calls[0] == ["taskkill", "/PID", "4242", "/F"]


def test_kill_port_holders_can_target_the_scheduler_lock_port():
    netstat_output = "  TCP    127.0.0.1:8801         0.0.0.0:0              LISTENING       5150\n"
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "netstat":
            return subprocess.CompletedProcess(cmd, 0, stdout=netstat_output, stderr="")
        return subprocess.CompletedProcess(cmd, 0)

    watchdog.kill_port_holders(watchdog.SCHEDULER_LOCK_PORT, runner=fake_runner)

    taskkill_calls = [c for c in calls if c[0] == "taskkill"]
    assert taskkill_calls == [["taskkill", "/PID", "5150", "/F"]]


def test_kill_port_holders_defaults_to_the_app_port():
    netstat_output = "  TCP    127.0.0.1:8001         0.0.0.0:0              LISTENING       7777\n"

    def fake_runner(cmd, **kwargs):
        if cmd[0] == "netstat":
            return subprocess.CompletedProcess(cmd, 0, stdout=netstat_output, stderr="")
        return subprocess.CompletedProcess(cmd, 0)

    calls = []
    watchdog.kill_port_holders(runner=lambda cmd, **kw: calls.append(cmd) or fake_runner(cmd, **kw))

    assert ["taskkill", "/PID", "7777", "/F"] in calls


def test_kill_port_holders_no_taskkill_when_nothing_listens():
    def fake_runner(cmd, **kwargs):
        if cmd[0] == "netstat":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0)

    calls = []
    watchdog.kill_port_holders(runner=lambda cmd, **kw: calls.append(cmd) or fake_runner(cmd, **kw))

    assert all(c[0] != "taskkill" for c in calls)


def test_kill_process_tree_uses_taskkill_with_tree_flag():
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    watchdog.kill_process_tree(4242, runner=fake_runner)

    assert calls == [["taskkill", "/T", "/F", "/PID", "4242"]]


def test_kill_process_tree_swallows_errors():
    def fake_runner(cmd, **kwargs):
        raise OSError("no such process")

    watchdog.kill_process_tree(4242, runner=fake_runner)  # must not raise


def test_start_app_returns_the_popen_handle():
    class _FakeProc:
        pid = 424242

    calls = {}

    def fake_popen(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs
        return _FakeProc()

    proc = watchdog.start_app(popen=fake_popen)

    assert proc is not None
    assert proc.pid == 424242
    assert calls["cmd"][0] == str(watchdog.VENV_PYTHON)
    assert "uvicorn" in calls["cmd"]
    assert "--port" in calls["cmd"] and watchdog.PORT in calls["cmd"]
    assert calls["kwargs"]["creationflags"] == watchdog.DETACHED


def test_start_app_returns_none_on_failure():
    def fake_popen(cmd, **kwargs):
        raise OSError("cannot launch")

    assert watchdog.start_app(popen=fake_popen) is None


# --- file hygiene (ASCII-safe: the mojibake this file replaced) ---------------------------


def test_script_source_is_pure_ascii():
    """The file this replaced decoded with U+FFFD replacement characters at several em
    dashes — a codepage mismatch under whatever last edited/ran it. A SYSTEM-owned scheduled
    task has no console codepage guarantee, so the script must never depend on one."""
    raw = _SCRIPT_PATH.read_bytes()
    raw.decode("ascii")  # raises UnicodeDecodeError on any non-ASCII byte


def test_main_is_not_invoked_by_importing_the_module():
    """The module is loaded under a name other than "__main__" above, so its
    `if __name__ == "__main__": sys.exit(main())` guard must not have fired — `main` stays a
    plain, uncalled function object (no background loop, no network, no subprocess)."""
    assert watchdog.__name__ != "__main__"
    assert callable(watchdog.main)


def test_manual_restart_marker_window():
    """restart_live.ps1 writes the marker; the watchdog stands down for START_TIMEOUT after it."""
    t = 1_000_000.0
    assert watchdog.manual_restart_in_progress(t, t + 10)
    assert watchdog.manual_restart_in_progress(t, t + watchdog.START_TIMEOUT - 1)
    assert not watchdog.manual_restart_in_progress(t, t + watchdog.START_TIMEOUT + 1)
    assert not watchdog.manual_restart_in_progress(None, t)


def test_restart_script_writes_the_marker_the_watchdog_reads():
    ps1 = (_SCRIPT_PATH.parent / "restart_live.ps1").read_text(encoding="utf-8")
    assert "restart_in_progress" in ps1
    assert watchdog.MANUAL_RESTART_MARKER.name == "restart_in_progress"
