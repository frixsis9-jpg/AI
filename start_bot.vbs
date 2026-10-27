' Double-click this to start the bot completely in the background: no cmd
' window, no console flash, nothing to keep open. It launches launcher.ps1
' (which starts main.py and auto-restarts it if it crashes) fully hidden.
' Check bot_status.vbs to see whether it's running, and stop_bot.bat to
' stop it.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir

command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptDir & "\launcher.ps1"""
shell.Run command, 0, False

MsgBox "Бот запускается в фоне. В Telegram придёт сообщение ""✅ PocketSignalBot запущен"", когда он будет готов (если в .env указан TELEGRAM_ADMIN_CHAT_ID). Проверить статус: bot_status.vbs. Остановить: stop_bot.bat.", 64, "PocketSignalBot"
