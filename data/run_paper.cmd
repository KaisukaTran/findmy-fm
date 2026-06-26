@echo off
REM FINDMY-FM paper server launcher (used by the "FINDMY-Paper" scheduled task).
REM Launches uvicorn DETACHED with its OWN hidden console (Start-Process -WindowStyle Hidden)
REM so it is NOT attached to the launching console — closing a PowerShell window / the IDE no
REM longer sends it CTRL+C (the bug that killed the earlier interactive-console run). The cmd +
REM powershell wrappers exit immediately; the uvicorn process keeps running on 127.0.0.1:8000,
REM logging to data\uvicorn.{out,err}.log. (Survives window/IDE close + RDP disconnect; a full
REM logoff/reboot still ends it — the AtLogOn task trigger restarts it on next logon.)
powershell -NoProfile -Command "Start-Process -FilePath 'D:\FINDMY\.venv\Scripts\python.exe' -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000' -WorkingDirectory 'D:\FINDMY' -RedirectStandardOutput 'D:\FINDMY\data\uvicorn.out.log' -RedirectStandardError 'D:\FINDMY\data\uvicorn.err.log' -WindowStyle Hidden"
