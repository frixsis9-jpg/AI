@echo off
rem Prevents Windows from going to sleep, so a bot started with start_bot.vbs
rem (patch8) keeps responding to Telegram at any hour, from any device -
rem "always active" only works if the PC itself stays awake. Run this once.
rem
rem To undo later: Windows Settings -> System -> Power & battery -> Screen
rem and sleep (this script only touches SLEEP, not the screen - the screen
rem turning off does NOT stop the bot, only actual system sleep does).
setlocal
cd /d "%~dp0"

echo Сохраняю текущие настройки сна на всякий случай (power_settings_backup.txt)...
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE > power_settings_backup.txt

echo Отключаю переход в спящий режим (и от сети, и от батареи)...
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0

echo Если это ноутбук - закрытие крышки по умолчанию тоже усыпляет систему.
echo Отключаю и это действие ("при закрытии крышки" -^> "ничего не делать")...
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT

echo.
echo Готово. Компьютер (и крышка ноутбука, если применимо) больше сами
echo не уснут - экран может гаснуть по таймеру как раньше, это не мешает.
echo.
echo Важно: это про СОН, а не про выход из Windows. Если вы выйдете из
echo учётной записи (Sign out) - Windows завершит и процесс бота вместе с
echo сессией. Экран блокировки (Win+L) - это НЕ выход из системы, бот
echo продолжит работать. Просто не разлогинивайтесь и не выключайте ПК.
pause
endlocal
