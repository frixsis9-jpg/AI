@echo off
rem Starts the bot using the venv created by setup_windows.bat.
setlocal

if not exist .venv\Scripts\activate.bat (
    echo Virtual environment not found. Run setup_windows.bat first.
    exit /b 1
)
if not exist .env (
    echo .env not found. Run setup_windows.bat first, then fill in .env.
    exit /b 1
)

call .venv\Scripts\activate.bat
python main.py
endlocal
