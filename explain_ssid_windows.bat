@echo off
rem Prints the real, installed library's own SSID validation code - no
rem network needed. See README, section "Если ни ssid, ни po_session нет".
setlocal

if not exist .venv\Scripts\activate.bat (
    echo Virtual environment not found. Run setup_windows.bat first.
    exit /b 1
)

call .venv\Scripts\activate.bat
python scripts\explain_ssid_requirements.py
endlocal
