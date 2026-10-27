@echo off
rem Stops a bot started with start_bot.vbs. Double-click this file.
setlocal
cd /d "%~dp0"

rem Tells launcher.ps1 not to restart the bot after this stop.
echo stop > stop.flag

if exist bot.pid (
    set /p BOTPID=<bot.pid
    taskkill /PID %BOTPID% /T /F >nul 2>&1
    del bot.pid >nul 2>&1
    echo Бот остановлен.
) else (
    echo Файл bot.pid не найден - похоже, бот и так не запущен.
)

rem stop.flag is cleaned up by launcher.ps1 itself once it notices it -
rem left alone here to avoid a race with that check.
pause
endlocal
