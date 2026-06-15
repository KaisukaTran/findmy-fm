# Live instance — production, FROZEN code (receives only reviewed merges; never edited in place).
# Starts on Binance TESTNET first (LIVE_USE_TESTNET=true) — no real funds until you flip it.
# Run from the LIVE worktree (d:\FINDMY-live).  Dashboard: http://127.0.0.1:8001
#
#   git worktree add ../FINDMY-live live      # one-time
#   cd d:\FINDMY-live ; copy .env.live.example .env ; <add TESTNET keys>
#   pwsh -File scripts/run_live.ps1
#
# Keys come from the live worktree's own .env (testnet) — NEVER committed. Without keys the
# instance boots but execution.live_enabled() stays false (behaves like a 2nd paper app).
$ErrorActionPreference = "Stop"
$env:DATABASE_URL        = "sqlite:///./data/live.db"   # separate book — never touches paper
$env:SCHEDULER_LOCK_PORT = "8802"                       # distinct lock → its own scheduler runs
$env:LIVE_TRADING        = "true"
$env:LIVE_USE_TESTNET    = "true"
& "D:\FINDMY\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8001
