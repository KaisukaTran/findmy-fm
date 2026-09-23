"""Keep the live FINDMY-FM instance alive on 127.0.0.1:8001.

WHY THIS EXISTS
    The live instance is launched directly - unlike paper, it has no scheduled task, and one
    cannot be registered without an elevated shell. On 2026-09-03 that cost two outages in a
    day: the machine rebooted and nothing brought the app back, then Windows closed it as a
    hung app at 09:59 after it stopped logging mid-cycle. The book sat unmanaged for 72
    minutes with six open positions and no stop-loss guard running.

WHY PYTHON AND NOT THE POWERSHELL VERSION
    A background powershell.exe started from an automation shell gets reaped as soon as
    that shell exits (verified: the loop ran fine in the foreground, wrote its start line, and
    was gone every time it was backgrounded). python.exe survives - that is exactly how
    uvicorn itself stays up here - so the watchdog is a Python process for the same reason.

WHAT IT DOES
    Every INTERVAL seconds: ask /health. Healthy -> do nothing. Unreachable, or reporting a
    stalled scheduler, for FAILURES_BEFORE_RESTART consecutive checks -> kill whatever holds
    the port (and any child of ours still starting) and launch a fresh uvicorn. After a
    launch, poll /health every START_POLL_INTERVAL seconds for up to START_TIMEOUT seconds
    instead of sleeping once and hoping - success requires scheduler_running: true AND not
    stalled, not just "the port answers".

2026-09-22 SPLIT-BRAIN OUTAGE (why the launch/restart logic changed)
    The app stopped answering at 19:35 local. This watchdog killed the port holder, launched
    a fresh uvicorn, waited a FIXED 25 seconds (START_GRACE), saw "STILL DOWN", and - after two
    more failed checks - launched ANOTHER uvicorn WITHOUT killing the first one (it only ever
    killed whatever was LISTENING on 8001, and the first process was still inside lifespan
    startup, not yet bound to the port). Both processes raced app.scheduler's cross-process
    singleton lock: one took the lock (port 8801) but then lost the socket bind (WinError
    10048) and exited, freeing the lock; the other bound :8001 but had already logged "another
    instance already holds the lock" and served HTTP with NO scheduler running - for about
    13.7 hours, because /health did not yet say so (see app.routes.health's stall_reason /
    app.config.Settings.health_boot_grace_sec, fixed the same day).
    The fix here: (1) a real Popen handle is kept for whatever THIS watchdog launched, and a
    still-alive previous launch is killed (whole process TREE, see kill_process_tree) before a
    new one is started; (2) a launch is never re-triggered while a previous one is still inside
    its own START_TIMEOUT; (3) a bounded restart budget (MAX_RESTARTS_PER_WINDOW per
    RESTART_WINDOW_SEC) stops a persistent, unfixable failure from spinning the process forever.

2026-09-23 CROSS-CHECK FIXES
    - health() now catches ANY exception, not a fixed tuple: it missed
      http.client.HTTPException (IncompleteRead / BadStatusLine on a truncated or malformed
      reply, e.g. mid-restart), which propagated out uncaught and killed main()'s loop until
      the next reboot. main() also now wraps each loop iteration in its own try/except, so a
      bug ANYWHERE in a tick logs and moves on (with the normal INTERVAL sleep still applied)
      instead of taking the whole watchdog down.
    - START_TIMEOUT raised 120s -> 300s: a real boot has been measured taking ~151s
      (launch to bind), which a 120s bound would have given up on before it ever finished.
    - kill_port_holders now also targets SCHEDULER_LOCK_PORT (8801) before every relaunch, not
      only PORT (8001): a hung process this watchdog never launched itself (so
      kill_process_tree has no PID to use) can hold app.scheduler's cross-process singleton
      lock forever, which would otherwise make every fresh launch lose the lock race and
      fail-fast (see app.config.Settings.scheduler_lock_fail_fast) in an infinite loop of
      restarts that can never succeed.
    - Corrected a wrong claim in kill_process_tree's docstring: killing the venv python.exe PID
      DOES kill its interpreter directly (verified) - it is not a launcher-plus-child-process
      pair. `/T` is kept anyway, defensively; see that docstring.

USAGE
    Started by the `FINDMY-Live-Watchdog` scheduled task (SYSTEM, AtStartup); can also be
    run by hand with the repo's venv interpreter. Paths follow this file, so the script
    keeps working wherever the repo is moved to.

    ASCII ONLY in this file, deliberately: reading these bytes back once produced U+FFFD
    replacement characters at every em dash (mojibake from a codepage mismatch under
    whatever launched/edited this file previously) - the exact failure class documented in
    scripts/restart_live.ps1's own header for the same reason. A SYSTEM-owned scheduled task
    has no reliable console codepage to assume, so this file never risks it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# Derived, never hardcoded: the watchdog must keep working after the worktrees are merged
# into one folder (and from whatever path it is copied to). ROOT is the repo that contains
# this script; the interpreter is whichever one is running it, which is the venv python
# because that is what the scheduled task launches.
ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = Path(sys.executable)
HEALTH_URL = "http://127.0.0.1:8001/health"
LOG = ROOT / "data" / "watchdog.log"
# Written by scripts/restart_live.ps1 (the FINDMY-Live-Restart task) the moment it starts a
# restart. That path launches uvicorn OUTSIDE this watchdog, and a ~151 s boot is long enough for
# two failed checks here - without this marker the watchdog would launch a redundant copy and,
# if the manual boot already held the lock, kill it (2026-09-23 round-3 cross-check).
MANUAL_RESTART_MARKER = ROOT / "data" / "restart_in_progress"
PORT = "8001"
# Must match app.config.Settings.scheduler_lock_port's default (8801) - duplicated here rather
# than imported, to keep this standalone SYSTEM-run script free of a live app.config import
# (it runs outside any guarantee of the app's own venv/working directory). If the app is ever
# configured with a non-default scheduler_lock_port, update this constant to match.
SCHEDULER_LOCK_PORT = "8801"

INTERVAL = 60.0              # seconds between steady-state checks
FAILURES_BEFORE_RESTART = 2  # consecutive bad checks before acting (never restart on one blip)
START_TIMEOUT = 300.0        # seconds to confirm a fresh launch actually came up (measured: a
                              # real boot took ~151s launch-to-bind; 120s gave up too early)
START_POLL_INTERVAL = 5.0    # seconds between /health polls while confirming a fresh launch

RESTART_WINDOW_SEC = 1800.0    # 30 minutes
MAX_RESTARTS_PER_WINDOW = 3    # give up (stop restarting) once this many happen inside the window

# Windows process-creation flags: detach the child so it outlives this watchdog and owns no
# console of ours (the same shape the manual launch uses).
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n"
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass  # a locked log file must never take the watchdog down
    print(line, end="", flush=True)


def health() -> dict | None:
    """Parsed /health, or None on ANY failure - unreachable, timed out, or a malformed reply.

    2026-09-23 cross-check: a narrower (urllib.error.URLError, OSError, ValueError,
    TimeoutError) tuple missed http.client.HTTPException (IncompleteRead / BadStatusLine on a
    truncated or malformed reply - e.g. the app mid-restart, answering with a half-written
    response) which propagated out of here uncaught and killed the watchdog's main loop until
    the next reboot. This function's entire contract is "never raise, treat any failure as
    unreachable" - so the catch is deliberately Exception, not a maintained list of exception
    types the underlying stdlib http/socket layers could someday grow another member of.
    """
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=10) as resp:  # noqa: S310 - fixed localhost URL
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    # Only a JSON OBJECT that carries our own "status" key is this app answering. A list/string/
    # number (or an unrelated service squatting on the port) must read as unreachable - a
    # non-dict used to raise AttributeError in classify() every tick without ever counting a
    # failure, and a bare {} used to count as healthy (2026-09-23 round-3 cross-check).
    if not isinstance(body, dict) or "status" not in body:
        return None
    return body


# ---------------------------------------------------------------------------------------
# Pure decision logic - no network, no subprocess, no clock reads. Unit-tested directly in
# tests/app/test_live_watchdog.py by importing this module by path.
# ---------------------------------------------------------------------------------------


def manual_restart_in_progress(marker_mtime: float | None, now_wall: float) -> bool:
    """True while a restart started by scripts/restart_live.ps1 is still inside START_TIMEOUT."""
    return marker_mtime is not None and 0 <= now_wall - marker_mtime < START_TIMEOUT


def _marker_mtime() -> float | None:
    try:
        return MANUAL_RESTART_MARKER.stat().st_mtime
    except OSError:
        return None


def classify(health_body: dict | None) -> tuple[bool, str]:
    """(healthy, reason) from a parsed /health body (or None when unreachable).

    Trusts the app's OWN verdict (scheduler_running / stalled / stall_reason - see
    app.routes.health) instead of re-deriving staleness thresholds here: the watchdog and the
    app must never disagree about what "stalled" means, or exactly the 2026-09-22 gap (the app
    said "ok", the watchdog had no way to know better) can recur in the other direction.
    """
    if health_body is None:
        return False, "unreachable"
    if health_body.get("stalled"):
        reason = health_body.get("stall_reason") or "stalled"
        return False, reason
    return True, ""


def is_confirmed_started(health_body: dict | None) -> bool:
    """Stricter than `classify`: used ONLY right after a fresh launch, to confirm the new
    process is not just answering HTTP but has actually taken over the scheduler. A bare
    "not stalled" is insufficient here on purpose - within health_boot_grace_sec a process
    that never started its scheduler at all also reads as "not stalled" (that grace window is
    exactly what let the 2026-09-22 twin look healthy), so a fresh launch is only accepted once
    it explicitly reports scheduler_running: true.

    2026-09-23 round-2 cross-check: `should_run: false` (see app.routes.health) means the
    OPERATOR stopped the scheduler in THIS process on purpose (scheduler_operator_stopped, or
    both scheduler_enabled/full_auto simply off) - waiting up to START_TIMEOUT for
    scheduler_running to become true would wait 300s for something that will never happen.
    HTTP answering and not stalled is "started" enough in that case. A body without the key at
    all (an older app that predates it) keeps the old strict behaviour instead of silently
    accepting HTTP-only as "started".
    """
    if health_body is None:
        return False
    if health_body.get("stalled"):
        return False
    if not health_body.get("should_run", True):
        return True
    return bool(health_body.get("scheduler_running"))


def prune_restart_log(restarts: list[float], now: float, window: float = RESTART_WINDOW_SEC) -> list[float]:
    """Restart timestamps still inside the trailing `window` seconds before `now`."""
    return [t for t in restarts if now - t < window]


def restart_budget_ok(
    restarts: list[float],
    now: float,
    *,
    window: float = RESTART_WINDOW_SEC,
    max_restarts: int = MAX_RESTARTS_PER_WINDOW,
) -> bool:
    """True if one more restart is allowed under the trailing-window budget."""
    return len(prune_restart_log(restarts, now, window)) < max_restarts


def still_starting(launch_started_at: float | None, now: float, timeout: float = START_TIMEOUT) -> bool:
    """True while a previous launch is still inside its own START_TIMEOUT window - a second
    restart must never fire on top of one that has not yet had its full chance to come up."""
    return launch_started_at is not None and (now - launch_started_at) < timeout


# ---------------------------------------------------------------------------------------
# Process control (impure - subprocess/OS calls). `runner`/`popen` are injectable seams so
# tests can exercise the call shape without spawning anything real.
# ---------------------------------------------------------------------------------------


def kill_port_holders(port: str = PORT, runner=subprocess.run) -> None:
    """Kill whatever still listens on *port* (default: PORT, the app itself) - a wedged
    process keeps the socket.

    2026-09-23 cross-check: also called for SCHEDULER_LOCK_PORT before every relaunch, not
    only PORT. A hung process this watchdog never launched itself (so kill_process_tree has no
    PID to use - a manual start, a previous watchdog generation, anything) can hold
    app.scheduler's cross-process singleton lock forever; without clearing that too, every
    fresh launch would lose the lock race and fail-fast (scheduler_lock_fail_fast) in an
    infinite loop of restarts that can never succeed.
    """
    try:
        out = runner(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return
    pids = {
        parts[-1]
        for line in out.splitlines()
        if f":{port}" in line and "LISTENING" in line
        for parts in [line.split()]
        if parts and parts[-1].isdigit()
    }
    for pid in pids:
        log(f"killing stale listener PID {pid} on port {port}")
        try:
            runner(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass


def kill_process_tree(pid: int, runner=subprocess.run) -> None:
    """Kill *pid* and its whole process tree.

    2026-09-23 cross-check correction: an earlier version of this docstring claimed the venv
    `python.exe` launched here was a launcher stub whose real interpreter survived killing that
    PID alone. Re-checked: that is wrong - killing the PID this watchdog holds (VENV_PYTHON's
    own process) kills its interpreter (and uvicorn) directly, no separate child involved.
    `/T` (tree) is kept anyway, defensively: `start_app` launches a plain
    `python -m uvicorn ...` today (a single process), but if that invocation ever grows a
    child (e.g. `--workers`/`--reload` spawning a second process, or some future change to how
    it is started), a single-PID kill would silently leave one behind. Costs nothing today,
    when there are no children to find.
    """
    try:
        runner(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def start_app(popen=subprocess.Popen) -> subprocess.Popen | None:
    """Launch uvicorn exactly the way a manual start does: venv python, live worktree cwd.
    Returns the Popen handle (None on failure) so the caller can track and, if needed, kill
    the WHOLE tree of exactly this launch later - the handle the pre-2026-09-22 version threw
    away, which is why it could not tell "already have one starting" from "nothing running"."""
    out = ROOT / "data" / "uvicorn.out.log"
    err = ROOT / "data" / "uvicorn.err.log"
    try:
        with out.open("ab") as fo, err.open("ab") as fe:
            proc = popen(
                [str(VENV_PYTHON), "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", PORT],
                cwd=str(ROOT), stdout=fo, stderr=fe, creationflags=DETACHED,
            )
        log(f"started uvicorn (PID {proc.pid})")
        return proc
    except OSError as exc:
        log(f"start failed: {exc}")
        return None


def wait_for_start(
    poll=health,
    sleep=time.sleep,
    clock=time.monotonic,
    timeout: float = START_TIMEOUT,
    interval: float = START_POLL_INTERVAL,
    child: subprocess.Popen | None = None,
) -> bool:
    """Poll /health every `interval` seconds for up to `timeout` seconds; returns as soon as
    `is_confirmed_started` is true. Replaces the old fixed `time.sleep(START_GRACE)` + one
    check - a slow-but-fine boot no longer reads as a failed restart, and a genuinely wedged
    one is not given more than `timeout` seconds either.

    2026-09-23 round-2 cross-check: also fails fast (returns False immediately, WITHOUT waiting
    out the rest of `timeout`) when *child* - the process this exact call just launched - has
    already exited. Polling /health for up to 300s for a process that crashed on import in the
    first second just delays the outer loop's next chance to react by five minutes; the exit
    code is logged so the failure is visible without digging through uvicorn's own log files.
    """
    def _child_exited() -> bool:
        if child is None:
            return False
        code = child.poll()
        if code is None:
            return False
        log(f"launched process exited early (code {code}) - not waiting out START_TIMEOUT")
        return True

    deadline = clock() + timeout
    while clock() < deadline:
        if _child_exited():
            return False
        if is_confirmed_started(poll()):
            return True
        sleep(interval)
    if _child_exited():
        return False
    return is_confirmed_started(poll())


def main() -> int:
    log(f"watchdog started (PID {os.getpid()}, every {INTERVAL:.0f}s)")
    failures = 0
    child: subprocess.Popen | None = None
    launch_started_at: float | None = None
    restart_log: list[float] = []

    while True:
        # 2026-09-23 cross-check: health() already never raises (broad catch, see its own
        # docstring), but everything ELSE in a tick - classify, the restart decision, the
        # subprocess calls - did not have that guarantee, and one uncaught exception here used
        # to kill the whole watchdog until the next reboot. Every iteration is wrapped so a bug
        # ANYWHERE in a tick logs and moves on, with the normal INTERVAL sleep still applied
        # (never a hot spin on a persistently-raising tick).
        try:
            now = time.monotonic()

            if still_starting(launch_started_at, now):
                # A launch is already in flight and inside its own START_TIMEOUT (defensive -
                # the blocking wait_for_start call below normally makes this unreachable, but a
                # second restart must never stack on top of one still proving itself either
                # way - and never kill a child that is alive and inside its own START_TIMEOUT).
                time.sleep(INTERVAL)
                continue

            if manual_restart_in_progress(_marker_mtime(), time.time()):
                # A manual restart (FINDMY-Live-Restart) is booting its own copy - never count
                # failures against it or launch a second one on top.
                failures = 0
                time.sleep(INTERVAL)
                continue

            healthy, reason = classify(health())
            if healthy:
                if failures:
                    log("health recovered")
                failures = 0
            else:
                failures += 1
                log(f"health unhealthy ({reason}) ({failures}/{FAILURES_BEFORE_RESTART})")

            if failures >= FAILURES_BEFORE_RESTART:
                now = time.monotonic()
                if not restart_budget_ok(restart_log, now):
                    log(
                        f"GIVING UP for {RESTART_WINDOW_SEC / 60:.0f} min - "
                        f"{len(prune_restart_log(restart_log, now))} restarts already happened "
                        f"in the last {RESTART_WINDOW_SEC / 60:.0f} min (budget exhausted); "
                        "will retry once the window clears"
                    )
                    failures = 0
                    time.sleep(INTERVAL)
                    continue

                if child is not None and child.poll() is None:
                    log(f"previous launch (PID {child.pid}) still alive - killing its tree first")
                    kill_process_tree(child.pid)
                kill_port_holders(PORT)
                # 8801 is THIS app's own scheduler singleton lock port (scheduler_lock_port) -
                # not shared with any other instance. The legacy run_paper.ps1 (8000/8801) is
                # disabled, so clearing 8801 here can only ever hit a stale holder from this
                # same app, never a live sibling.
                kill_port_holders(SCHEDULER_LOCK_PORT)
                time.sleep(3)

                restart_log.append(now)
                restart_log = prune_restart_log(restart_log, now)
                child = start_app()
                launch_started_at = time.monotonic()
                ok = wait_for_start(child=child)
                log("after restart: {}".format("OK" if ok else "STILL DOWN"))
                launch_started_at = None
                failures = 0
        except Exception as exc:
            log(f"watchdog tick failed: {type(exc).__name__}: {exc}")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
