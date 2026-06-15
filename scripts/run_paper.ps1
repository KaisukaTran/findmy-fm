# Paper instance — the staging / experiment environment (all dev + testing happens here).
# NO real exchange keys and LIVE_TRADING=false, so it can never place a real order.
# Run from the PAPER worktree (d:\FINDMY).  Dashboard: http://127.0.0.1:8000
#
#   pwsh -File scripts/run_paper.ps1
#
$ErrorActionPreference = "Stop"
$env:DATABASE_URL       = "sqlite:///./data/findmy.db"   # the existing paper book (kept as-is)
$env:SCHEDULER_LOCK_PORT = "8801"
$env:LIVE_TRADING        = "false"
& "D:\FINDMY\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
