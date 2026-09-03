@echo off
REM ---------------------------------------------------------------------------
REM PAPER LAUNCH DISABLED 2026-08-30 — the system was consolidated onto ONE
REM instance for the month of Binance-testnet demo before real funds: the
REM testnet app on :8001 (D:\FINDMY-live). Two apps would also share one IP's
REM exchange rate limit.
REM
REM This file is a no-op on purpose. The "FINDMY-Paper" scheduled task still
REM exists and still runs this, so it starts nothing — that is how paper stays
REM off across logons without needing an elevated shell to disable the task.
REM
REM TO BRING PAPER BACK: restore the real launcher and start it again:
REM     copy /Y "D:\FINDMY\data\run_paper.cmd.enabled-backup" "D:\FINDMY\data\run_paper.cmd"
REM     schtasks /Run /TN FINDMY-Paper
REM Its book (data\findmy.db) was never touched: 51 completed sessions,
REM 11 open positions, +$72.13 simulated at the time of shutdown.
REM ---------------------------------------------------------------------------
echo FINDMY-Paper launcher is disabled - see the comments in this file.
exit /b 0
