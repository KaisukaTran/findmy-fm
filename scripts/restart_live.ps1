# Restart the live FINDMY-FM instance on 127.0.0.1:8001.
#
# WHY A FILE AND NOT AN INLINE TASK COMMAND
#     The scheduled task that calls this used to carry the whole restart as an inline
#     -Command string. That cannot be typed safely: schtasks splits the argument at the
#     first pipe, so the task stored only the fragment up to "|" and the rest of the
#     pipeline executed in the caller's own shell instead. Escaped inner quotes and $_
#     are equally unsafe through PowerShell's native-argument parser. A file has no
#     quoting problem at all, and it can be read before it is trusted.
#
# WHY THE TASK EXISTS
#     The live instance is launched by a SYSTEM-owned watchdog, so it runs above the
#     automation agent's medium-integrity token: Stop-Process and taskkill both answer
#     "Access is denied", and the agent cannot even read the process owner. This task
#     runs at HighestAvailable, so triggering it is the one way an unelevated caller can
#     get the live app restarted.
#
# ASCII ONLY, and no backtick line continuations: PowerShell 5.1 reads a BOM-less .ps1
# as ANSI (one em dash has broken a parse here before), and a backtick followed by CRLF
# escapes the CR and swallows the next line.

$ErrorActionPreference = 'Stop'

$Root       = 'D:\FINDMY'
$Python     = Join-Path $Root '.venv\Scripts\python.exe'
$Port       = 8001
$OutLog     = Join-Path $Root 'data\uvicorn.out.log'
$ErrLog     = Join-Path $Root 'data\uvicorn.err.log'
$RestartLog = Join-Path $Root 'data\restart_live.log'

function Write-Log([string]$Message) {
    # File only, deliberately no Write-Output. Under Task Scheduler there is no console, but
    # if this script is ever piped into something that stops draining stdout the process
    # blocks on a full pipe buffer and the task instance never finishes - with
    # MultipleInstancesPolicy=IgnoreNew that would silently block every later restart.
    # Observed exactly that while testing. The log file is the real trail anyway.
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    try { Add-Content -Path $RestartLog -Value $line -Encoding utf8 } catch { }
}

Write-Log 'restart requested'

if (-not (Test-Path $Python)) {
    # The venv interpreter is not optional: the system python has no uvicorn and no ccxt,
    # so launching with it leaves the port free and the book unmanaged.
    Write-Log "ABORT: interpreter not found at $Python"
    return
}

# Stop whatever holds the port. Guarded because Get-NetTCPConnection returns nothing when
# the app is already down, and Stop-Process refuses a null Id.
$conns = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($conns.Count -eq 0) {
    Write-Log "no listener on $Port (already stopped)"
} else {
    # NOT $pid: that is a read-only automatic variable (this shell's own process id) and
    # assigning to it in a foreach throws at runtime, which a syntax check does not catch.
    foreach ($procId in ($conns | Select-Object -ExpandProperty OwningProcess -Unique)) {
        if ($null -eq $procId) { continue }
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Log "stopped PID $procId"
        } catch {
            Write-Log "FAILED to stop PID ${procId}: $($_.Exception.Message)"
        }
    }
    Start-Sleep -Seconds 3
}

# Both redirects are mandatory, not cosmetic: a detached child started without them is
# reaped when the launching shell's command ends, which is why every earlier attempt at a
# background process here died silently.
# Single line on purpose: a backtick continuation followed by CRLF escapes the CR instead
# of the newline and the parse dies with "Missing closing }". This file must survive being
# edited by anything that normalises line endings.
$uvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8001')
Start-Process -FilePath $Python -ArgumentList $uvicornArgs -WorkingDirectory $Root -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -WindowStyle Hidden
Write-Log 'uvicorn launched'

# Verify rather than assume. A restart silently disarms nothing today (full_auto is
# persisted in runtime_config), but a process that binds and then dies on an import error
# would otherwise look like success.
$deadline = (Get-Date).AddSeconds(45)
$ok = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    try {
        $body = (Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 5).Content
        Write-Log "health: $body"
        $ok = $true
        break
    } catch { }
}
if (-not $ok) { Write-Log 'WARNING: no healthy response within 45s' }
