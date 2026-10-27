@echo off
rem Tests ONE candidate credential (a cookie value, or a full auth frame)
rem against the real Pocket Option server - see README, section "Если ни
rem ssid, ни po_session нет: поиск и диагностика". Usage:
rem   probe_ssid_windows.bat "<cookie value>"
setlocal

if not exist .venv\Scripts\activate.bat (
    echo Virtual environment not found. Run setup_windows.bat first.
    exit /b 1
)

call .venv\Scripts\activate.bat
python scripts\probe_ssid_candidate.py %*
endlocal
