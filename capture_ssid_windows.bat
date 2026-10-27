@echo off
rem Opens a real, visible browser window and captures the Pocket Option
rem auth frame automatically once you log in yourself - see README, section
rem "Автоматический захват (без ручного поиска в DevTools)". Needs an extra
rem one-time install: pip install playwright && playwright install chromium
setlocal

if not exist .venv\Scripts\activate.bat (
    echo Virtual environment not found. Run setup_windows.bat first.
    exit /b 1
)

call .venv\Scripts\activate.bat
python scripts\capture_ssid_playwright.py
endlocal
