@echo off
rem One-off fix: installs the aiohttp-socks package needed for
rem TELEGRAM_PROXY_URL (socks5://...) to work. Run this once, then restart
rem the bot (stop_bot.bat, then start_bot.vbs).
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo .venv not found - run setup_windows first.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m pip install "aiohttp-socks>=0.9,<1"
if errorlevel 1 (
    echo.
    echo Installation failed - see the error above.
    pause
    exit /b 1
)

echo.
echo Done. Now: stop_bot.bat, then start_bot.vbs.
pause
endlocal
