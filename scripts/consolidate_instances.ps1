# Collapse the two-process mess back to ONE app instance on 127.0.0.1:8001.
#
# WHY THIS EXISTS (2026-09-06)
#     After the 09-03 worktree merge there is only one folder, D:\FINDMY, and its .env is the
#     LIVE one (LIVE_TRADING=true, data/live.db, scheduler lock 8802). The AtLogOn task
#     "FINDMY-Paper" still launched data\run_paper.cmd, and that launcher carried NO environment
#     of its own -> at every logon it started a SECOND LIVE process on port 8000 against the same
#     book. Whichever of the two started first won the singleton lock, so on 2026-09-05 the
#     scheduler, the 90s position guard and the WS feed all lived on :8000 while the watchdog
#     (which only polls :8001) was guarding the idle twin. Both also ran a Telegram command
#     poller on the same bot token and wrote to the same uvicorn log files.
#     run_paper.cmd now sets its own paper env, so it can no longer produce a live twin. This
#     script clears the twin that is already running and hands :8001 back the scheduler.
#
# RUN IT FROM AN ELEVATED POWERSHELL:
#     powershell -NoProfile -ExecutionPolicy Bypass -File D:\FINDMY\scripts\consolidate_instances.ps1
# Elevation is needed for two of the steps: the :8001 process is launched by the SYSTEM-owned
# watchdog task, and disabling a scheduled task needs an admin token. The script says what it
# could not do rather than pretending it succeeded.
#
# ASCII ONLY, and no backtick line continuations - PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
# and a backtick before CRLF escapes the CR and swallows the next line.

$ErrorActionPreference = 'Stop'

$Root       = 'D:\FINDMY'
$RestartPs1 = Join-Path $Root 'scripts\restart_live.ps1'
$LogFile    = Join-Path $Root 'data\consolidate.log'

function Say([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    Write-Host $line
    try { Add-Content -Path $LogFile -Value $line -Encoding utf8 } catch { }
}

function Listener([int]$Port) {
    # Returns the owning PIDs listening on $Port, or an empty array. Get-NetTCPConnection
    # returns nothing (not an error) when the port is free, hence the array subexpression.
    @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Say ("start (elevated={0})" -f $elevated)
if (-not $elevated) { Say 'WARNING: not elevated - steps 1 and 3 will probably be denied' }

# --- 1. stop the AtLogOn task from starting a second app at every logon -------------------
try {
    $paper = Get-ScheduledTask -TaskName 'FINDMY-Paper' -ErrorAction Stop
    if ($paper.State -eq 'Disabled') {
        Say 'step 1: FINDMY-Paper already disabled'
    } else {
        Disable-ScheduledTask -TaskName 'FINDMY-Paper' -ErrorAction Stop | Out-Null
        Say 'step 1: FINDMY-Paper disabled (paper no longer starts at logon)'
    }
} catch {
    Say ("step 1 FAILED: {0}" -f $_.Exception.Message)
}

# --- 2. kill the twin on :8000 -------------------------------------------------------------
# Do this BEFORE restarting :8001: the singleton lock (127.0.0.1:8802) is taken at startup and
# never released while the holder lives, so :8001 can only pick up the scheduler once :8000 is
# gone. NOT $pid - that is this shell's own read-only process id.
$twin = Listener 8000
if ($twin.Count -eq 0) {
    Say 'step 2: nothing listening on 8000'
} else {
    foreach ($procId in $twin) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Say ("step 2: stopped PID {0} on :8000" -f $procId)
        } catch {
            Say ("step 2 FAILED for PID {0}: {1}" -f $procId, $_.Exception.Message)
        }
    }
    Start-Sleep -Seconds 3
}

# --- 3. restart :8001 so it takes the scheduler lock ---------------------------------------
# restart_live.ps1 kills the port holder and relaunches detached with both redirects (a child
# started without them is reaped when the launching command ends), then verifies /health.
if (-not (Test-Path $RestartPs1)) {
    Say ("step 3 ABORT: {0} not found" -f $RestartPs1)
    return
}
Say 'step 3: calling restart_live.ps1'
& $RestartPs1
Say 'step 3: restart_live.ps1 returned'

# --- 4. verify: one listener, and it is the one running the scheduler ----------------------
$after8000 = Listener 8000
$after8001 = Listener 8001
Say ("step 4: listeners 8000=[{0}] 8001=[{1}]" -f ($after8000 -join ','), ($after8001 -join ','))

$healthy = $false
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    try {
        $body = (Invoke-WebRequest -Uri 'http://127.0.0.1:8001/health' -UseBasicParsing -TimeoutSec 5).Content
        $h = $body | ConvertFrom-Json
        Say ("step 4: /health {0}" -f $body)
        if ($h.scheduler_running) { $healthy = $true; break }
    } catch { }
    Start-Sleep -Seconds 3
}

if ($healthy -and $after8000.Count -eq 0) {
    Say 'DONE: single instance on :8001, scheduler running.'
} elseif ($healthy) {
    Say 'PARTIAL: :8001 owns the scheduler but something still listens on :8000 - check it.'
} else {
    Say 'PROBLEM: :8001 did not report scheduler_running within 60s. Check data\uvicorn.err.log.'
}
