# Runs the bot hidden in the background, restarting it automatically if it
# crashes, and refuses to start a second copy if one is already running.
# Not meant to be double-clicked directly — start_bot.vbs launches this with
# no visible window at all. See stop_bot.bat to stop it and
# bot_status.vbs to check whether it's running, both without opening cmd.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$envFile = Join-Path $root ".env"
$logFile = Join-Path $root "bot.log"
$errLogFile = Join-Path $root "bot_error.log"
$launcherLog = Join-Path $root "bot_launcher.log"
$pidFile = Join-Path $root "bot.pid"
$stopFlag = Join-Path $root "stop.flag"

# $logFile/$errLogFile are python's own redirected stdout/stderr - Start-Process
# recreates (truncates) them on every restart. $launcherLog is written only
# via Log() below (Add-Content, never touched by Start-Process) and keeps the
# full history across restarts on purpose - keep the two separate, or a
# restart would silently wipe out the launcher's own history too.

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $launcherLog -Value $line
}

Remove-Item $stopFlag -ErrorAction SilentlyContinue

if (-not (Test-Path $venvPython)) {
    Log "ОШИБКА: .venv не найден. Сначала запустите setup_windows.bat."
    exit 1
}
if (-not (Test-Path $envFile)) {
    Log "ОШИБКА: .env не найден. Сначала запустите setup_windows.bat и заполните .env."
    exit 1
}

# Don't start a second copy on top of an already-running one. This used to
# be a plain "check bot.pid, then start" - but that has a race: double-click
# start_bot.vbs twice in quick succession (or once while the previous run
# hadn't written bot.pid yet) and BOTH launcher.ps1 processes can pass the
# check before either writes the file, so both start main.py - two copies
# both polling Telegram with the same bot token fight each other forever
# (TelegramConflictError in bot_error.log, bot looks completely "frozen"
# even though something is technically running). A named Mutex is atomic -
# only one process can ever hold it, so this can no longer happen.
$mutex = New-Object System.Threading.Mutex($false, "PocketSignalBotLauncherMutex")
if (-not $mutex.WaitOne(0)) {
    Log "Другой экземпляр лаунчера уже запущен (mutex занят) - выхожу."
    exit 0
}

try {
    Log "=== Запуск лаунчера ==="
    $failCount = 0

    while ($true) {
        Log "Старт main.py (попытка подряд без успеха: $failCount)"
        $startedAt = Get-Date
        $proc = Start-Process -FilePath $venvPython -ArgumentList "main.py" -WorkingDirectory $root `
            -RedirectStandardOutput $logFile -RedirectStandardError $errLogFile `
            -WindowStyle Hidden -PassThru
        Set-Content -Path $pidFile -Value $proc.Id
        $proc.WaitForExit()
        $exitCode = $proc.ExitCode
        $ranFor = (Get-Date) - $startedAt
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        Log ("main.py завершился с кодом {0} (работал {1:N0} сек.)" -f $exitCode, $ranFor.TotalSeconds)

        if (Test-Path $stopFlag) {
            Log "Получен запрос на остановку - выхожу без перезапуска."
            Remove-Item $stopFlag -ErrorAction SilentlyContinue
            break
        }

        if ($exitCode -eq 0) {
            Log "Чистое завершение (код 0) - не перезапускаю."
            break
        }

        # A crash within the first 20 seconds almost always means a config
        # problem (bad .env, bad SSID, no network) rather than a transient
        # blip, so back off harder to avoid hammering the Pocket
        # Option/Telegram APIs in a tight loop while nobody's watching.
        if ($ranFor.TotalSeconds -lt 20) {
            $failCount++
        } else {
            $failCount = 0
        }

        if ($failCount -ge 5) {
            Log "5 быстрых падений подряд - жду 5 минут перед следующей попыткой."
            Start-Sleep -Seconds 300
            $failCount = 0
        } else {
            Start-Sleep -Seconds 15
        }
    }

    Log "=== Лаунчер завершён ==="
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
