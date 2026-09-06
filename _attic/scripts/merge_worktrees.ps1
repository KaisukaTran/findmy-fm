# Merge the three FINDMY-FM worktrees into ONE folder: D:\FINDMY, branch `live`, port 8001.
#
# RUN THIS FROM AN ELEVATED POWERSHELL ("Run as Administrator"). It cannot work otherwise:
# the app and its watchdog run as SYSTEM (the scheduled task), so a normal shell can neither
# stop them nor re-register the task - and deleting D:\FINDMY-live while the app is still
# serving from it would take the live trading database with it.
#
# WHY D:\FINDMY IS THE SURVIVOR
#   It is the repository's main worktree (it owns .git) and it already holds .venv, which every
#   instance uses. The other two are `git worktree` checkouts and can be removed cleanly; their
#   branches stay in the repo and can be checked out again at any time.
#
# WHAT IT DOES, IN ORDER
#   1. stop the watchdog task, then the app
#   2. back up both databases and the paper .env
#   3. move the LIVE .env and live.db (+ WAL/SHM) into D:\FINDMY
#   4. remove the two extra worktrees
#   5. check `live` out in D:\FINDMY
#   6. re-register the watchdog task against the new path
#   7. start the app and verify /health
#
# Nothing is deleted before the copies are verified, and the script stops at the first error.

$ErrorActionPreference = 'Stop'

$MAIN = 'D:\FINDMY'
$LIVE = 'D:\FINDMY-live'
$TEST = 'D:\FINDMY-testnet'
$TASK = 'FINDMY-Live-Watchdog'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must run from an ELEVATED PowerShell (Run as Administrator)."
}

Step 1 'Stopping the watchdog task and the app'
try { Stop-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue } catch {}
try { Disable-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue | Out-Null } catch {}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and ($_.CommandLine -like '*live_watchdog*' -or $_.CommandLine -like '*uvicorn*8001*') } |
    ForEach-Object { Write-Host "    killing PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -Confirm:$false }
Start-Sleep -Seconds 4
if (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue) {
    throw "Something is still listening on 8001 - stop it before continuing."
}

Step 2 'Backing up databases and the paper .env'
Copy-Item "$LIVE\data\live.db" "$LIVE\data\live.db.bak-merge-$stamp" -Force
if (Test-Path "$MAIN\data\findmy.db") { Copy-Item "$MAIN\data\findmy.db" "$MAIN\data\findmy.db.bak-merge-$stamp" -Force }
if (Test-Path "$MAIN\.env") { Copy-Item "$MAIN\.env" "$MAIN\.env.paper-backup-$stamp" -Force }
Write-Host '    backups written'

Step 3 'Moving the live .env and database into D:\FINDMY'
Copy-Item "$LIVE\.env" "$MAIN\.env" -Force
foreach ($f in @('live.db', 'live.db-wal', 'live.db-shm', 'soak_start.json', 'watchdog.log')) {
    if (Test-Path "$LIVE\data\$f") { Copy-Item "$LIVE\data\$f" "$MAIN\data\$f" -Force }
}
if (-not (Test-Path "$MAIN\data\live.db")) { throw 'live.db did not copy - aborting before anything is removed.' }
$src = (Get-Item "$LIVE\data\live.db").Length
$dst = (Get-Item "$MAIN\data\live.db").Length
if ($src -ne $dst) { throw "live.db copy size mismatch ($src vs $dst) - aborting." }
Write-Host "    live.db copied ($dst bytes), .env replaced (paper copy kept as .env.paper-backup-$stamp)"

Step 4 'Removing the two extra worktrees'
Set-Location $MAIN
git worktree remove --force $LIVE
git worktree remove --force $TEST
git worktree prune
git worktree list

Step 5 'Checking out `live` in D:\FINDMY'
git checkout live
git log --oneline -1

Step 6 "Re-registering the $TASK task against the new path"
$action = New-ScheduledTaskAction -Execute "$MAIN\.venv\Scripts\python.exe" -Argument "$MAIN\scripts\live_watchdog.py" -WorkingDirectory $MAIN
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TASK -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Enable-ScheduledTask -TaskName $TASK | Out-Null
Start-ScheduledTask -TaskName $TASK

Step 7 'Waiting for the app to come up on 8001'
$deadline = (Get-Date).AddSeconds(180)
$up = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    try {
        $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8001/health' -TimeoutSec 8
        if ($h.status -eq 'ok') { $up = $true; break }
    } catch {}
}
if ($up) {
    Write-Host "`nDONE - app healthy from $MAIN" -ForegroundColor Green
    $h | ConvertTo-Json -Compress
    Write-Host "`nSanity checks worth a glance:"
    Write-Host "  git -C $MAIN log --oneline -1        (should be the live branch head)"
    Write-Host "  Get-Content $MAIN\data\watchdog.log -Tail 5"
    Write-Host "  the old folders are gone: $LIVE, $TEST"
} else {
    Write-Warning "The app did not answer within 180s. Start it by hand and check the logs:"
    Write-Warning "  $MAIN\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001"
    Write-Warning "  Get-Content $MAIN\data\uvicorn.err.log -Tail 30"
}
