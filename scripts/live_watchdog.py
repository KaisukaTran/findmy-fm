"""Keep the live FINDMY-FM instance alive on 127.0.0.1:8001.

WHY THIS EXISTS
    The live instance is launched directly — unlike paper, it has no scheduled task, and one
    cannot be registered without an elevated shell. On 2026-09-03 that cost two outages in a
    day: the machine rebooted and nothing brought the app back, then Windows closed it as a
    hung app at 09:59 after it stopped logging mid-cycle. The book sat unmanaged for 72
    minutes with six open positions and no stop-loss guard running.

WHY PYTHON AND NOT THE POWERSHELL VERSION
    A background ``powershell.exe`` started from an automation shell gets reaped as soon as
    that shell exits (verified: the loop ran fine in the foreground, wrote its start line, and
    was gone every time it was backgrounded). ``python.exe`` survives — that is exactly how
    uvicorn itself stays up here — so the watchdog is a Python process for the same reason.

WHAT IT DOES
    Every ``INTERVAL`` seconds: ask /health. Healthy -> do nothing. Unreachable, or reporting a
    stalled scheduler, for ``FAILURES_BEFORE_RESTART`` consecutive checks -> kill whatever
    uvicorn is holding the port and start a fresh one. Two consecutive failures are required so
    a single slow response never triggers a restart during a heavy scan.

USAGE
    Started by the `FINDMY-Live-Watchdog` scheduled task (SYSTEM, AtStartup); can also be
    run by hand with the repo's venv interpreter. Paths follow this file, so the script
    keeps working wherever the repo is moved to.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
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
PORT = "8001"

INTERVAL = 60.0            # seconds between checks
FAILURES_BEFORE_RESTART = 2  # consecutive bad checks before acting (never restart on one blip)
START_GRACE = 25.0         # seconds to let a fresh process bind and answer

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
    """Parsed /health, or None when the app does not answer."""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=10) as resp:  # noqa: S310 — fixed localhost URL
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def kill_port_holders() -> None:
    """Kill whatever still listens on 8001 (a wedged process keeps the socket)."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return
    pids = {
        parts[-1]
        for line in out.splitlines()
        if f":{PORT}" in line and "LISTENING" in line
        for parts in [line.split()]
        if parts and parts[-1].isdigit()
    }
    for pid in pids:
        log(f"killing stale listener PID {pid}")
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass


def start_app() -> None:
    """Launch uvicorn exactly the way a manual start does: venv python, live worktree cwd."""
    out = ROOT / "data" / "uvicorn.out.log"
    err = ROOT / "data" / "uvicorn.err.log"
    try:
        with out.open("ab") as fo, err.open("ab") as fe:
            subprocess.Popen(
                [str(VENV_PYTHON), "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", PORT],
                cwd=str(ROOT), stdout=fo, stderr=fe, creationflags=DETACHED,
            )
        log("started uvicorn")
    except OSError as exc:
        log(f"start failed: {exc}")


def main() -> int:
    log(f"watchdog started (PID {__import__('os').getpid()}, every {INTERVAL:.0f}s)")
    failures = 0
    while True:
        h = health()
        if h is None:
            failures += 1
            log(f"health unreachable ({failures}/{FAILURES_BEFORE_RESTART})")
        elif h.get("stalled"):
            # `stalled` is reported by /health when a loop has not completed within a generous
            # multiple of its own interval — a process that answers HTTP while its scheduler is
            # wedged is exactly the 09:51 failure, and it must count as unhealthy.
            failures += 1
            log(f"health says STALLED ({failures}/{FAILURES_BEFORE_RESTART}): "
                f"cycle={h.get('last_cycle_seconds_ago')}s guard={h.get('guard_seconds_ago')}s")
        else:
            if failures:
                log("health recovered")
            failures = 0

        if failures >= FAILURES_BEFORE_RESTART:
            kill_port_holders()
            time.sleep(3)
            start_app()
            time.sleep(START_GRACE)
            after = health()
            log(f"after restart: {'OK' if after else 'STILL DOWN'}")
            failures = 0

        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
