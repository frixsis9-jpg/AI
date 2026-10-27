@echo off
rem Kills EVERY running copy of this bot (main.py) AND every running copy of
rem the launcher (launcher.ps1) that watches over it - not just the one
rem bot.pid happens to point at right now. Use this if the bot seems
rem "stuck"/silent and stop_bot.bat + start_bot.vbs didn't fix it - that
rem usually means two or more copies got started and are fighting over the
rem same Telegram connection (TelegramConflictError in bot_error). Safe to
rem run any time, including when nothing is actually running.
rem
rem IMPORTANT: launcher.ps1's whole job is to restart main.py the moment it
rem exits - so killing only python.exe here does nothing lasting if a
rem launcher.ps1 (powershell.exe) is still alive: it just spawns a fresh
rem main.py seconds later. This is exactly what a launcher started BEFORE an
rem update to launcher.ps1 will keep doing forever, since a process already
rem running executes the copy of the script it started with - overwriting
rem launcher.ps1 on disk does not change what an already-running instance is
rem executing, mutex protection included. So: kill launchers FIRST, then
rem the bot processes they were watching.
setlocal
cd /d "%~dp0"

echo Stopping every PocketSignalBot launcher (launcher.ps1)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*launcher.ps1*' -and $_.CommandLine -like '*PocketSignalBot*' } | ForEach-Object { Write-Host ('Killing launcher PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo Stopping every PocketSignalBot process (main.py)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*main.py*' -and $_.CommandLine -like '*PocketSignalBot*' } | ForEach-Object { Write-Host ('Killing PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

if exist bot.pid del bot.pid
if exist stop.flag del stop.flag

echo.
echo Done. All launchers and all copies of the bot stopped. Now run
echo start_bot.vbs ONCE to start a single clean copy.
pause
endlocal
