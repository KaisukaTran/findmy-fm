@echo off
REM FINDMY-FM PAPER server launcher (task "FINDMY-Paper", currently DISABLED on purpose).
REM
REM 2026-09-06: this file used to launch uvicorn with NO environment of its own. With only one
REM worktree left (D:\FINDMY on branch `live`, .env = LIVE_TRADING/live.db/lock 8802), that made
REM the "paper" task start a SECOND LIVE instance on port 8000 which stole the scheduler lock
REM from the real live app on 8001. The env below is now explicit, so this launcher can only
REM ever be paper no matter what .env says. Do not remove these lines.
set "DATABASE_URL=sqlite:///./data/findmy.db"
set "SCHEDULER_LOCK_PORT=8801"
set "LIVE_TRADING=false"
set "TELEGRAM_POLL_COMMANDS=false"
REM Launches uvicorn DETACHED with its OWN hidden console so closing a PowerShell window / the
REM IDE no longer sends it CTRL+C. Logs to data\uvicorn.paper.{out,err}.log — the live instance
REM owns data\uvicorn.{out,err}.log; sharing them mixed two servers into one file.
powershell -NoProfile -Command "Start-Process -FilePath 'D:\FINDMY\.venv\Scripts\python.exe' -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000' -WorkingDirectory 'D:\FINDMY' -RedirectStandardOutput 'D:\FINDMY\data\uvicorn.paper.out.log' -RedirectStandardError 'D:\FINDMY\data\uvicorn.paper.err.log' -WindowStyle Hidden"
