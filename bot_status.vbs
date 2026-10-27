' Double-click to check, without opening cmd, whether the bot (started via
' start_bot.vbs) is currently running.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pidFile = scriptDir & "\bot.pid"

running = False
if fso.FileExists(pidFile) Then
    Set f = fso.OpenTextFile(pidFile, 1)
    pid = Trim(f.ReadLine())
    f.Close
    On Error Resume Next
    Set procs = GetObject("winmgmts:").ExecQuery("SELECT ProcessId FROM Win32_Process WHERE ProcessId = " & pid)
    On Error Goto 0
    if procs.Count > 0 Then
        running = True
    End If
End If

if running Then
    MsgBox "Бот работает (PID " & pid & ").", 64, "PocketSignalBot - статус"
Else
    MsgBox "Бот не запущен." & vbCrLf & "Запустить: start_bot.vbs", 48, "PocketSignalBot - статус"
End If
