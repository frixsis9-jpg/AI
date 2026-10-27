@echo off
rem Sets up the project for local use on Windows: creates a venv, installs
rem dependencies, and creates .env from .env.example if it doesn't exist yet.
rem Run this once (double-click it, or run it from cmd.exe), then edit .env,
rem then use run_windows.bat to start the bot.
setlocal enabledelayedexpansion

rem Prefer Python 3.11 via the py launcher (the version this project is
rem tested against) but don't hard-fail if it isn't installed - fall back to
rem whatever Python 3.x IS registered with the launcher (e.g. 3.14), since a
rem missing exact-version match used to make this whole script fail outright
rem even when a perfectly usable Python was already on the machine.
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3.11 -c "pass" >nul 2>nul
    if !ERRORLEVEL! EQU 0 (
        set "PYLAUNCHER=py -3.11"
    ) else (
        echo Python 3.11 not found via the py launcher - using the latest installed Python 3 instead.
        set "PYLAUNCHER=py -3"
    )
) else (
    set "PYLAUNCHER=python"
)

echo Using: %PYLAUNCHER%
%PYLAUNCHER% -m venv .venv
if errorlevel 1 (
    echo.
    echo Could not create the virtual environment.
    echo Make sure Python 3.10 or newer is installed from https://python.org
    echo and that "Add python.exe to PATH" was checked during install.
    exit /b 1
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency installation failed - see the error above.
    exit /b 1
)

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Created .env from .env.example.
)

echo.
echo Setup complete.
echo Next: edit .env with your TELEGRAM_BOT_TOKEN, POCKET_OPTION_SSID
echo (and optionally ANTHROPIC_API_KEY), then run run_windows.bat
endlocal
