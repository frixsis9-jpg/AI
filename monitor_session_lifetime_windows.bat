@echo off
rem Leaves a lightweight probe running against Pocket Option to measure
rem empirically how long your captured SSID actually stays valid - see
rem README, "24/7 без присмотра: что делать, когда SSID протухнет".
rem Needs a real POCKET_OPTION_SSID already set in .env. Usage:
rem   monitor_session_lifetime_windows.bat
rem   monitor_session_lifetime_windows.bat --interval 600
setlocal

if not exist .venv\Scripts\activate.bat (
    echo Virtual environment not found. Run setup_windows.bat first.
    exit /b 1
)

call .venv\Scripts\activate.bat
python scripts\monitor_session_lifetime.py %*
endlocal
