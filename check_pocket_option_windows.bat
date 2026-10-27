@echo off
rem Manual real-connection sanity check (see README) - needs a real
rem POCKET_OPTION_SSID already set in .env. Usage:
rem   check_pocket_option_windows.bat EUR_USD M5
setlocal

if not exist .venv\Scripts\activate.bat (
    echo Virtual environment not found. Run setup_windows.bat first.
    exit /b 1
)

call .venv\Scripts\activate.bat
python scripts\check_pocket_option_connection.py %*
endlocal
