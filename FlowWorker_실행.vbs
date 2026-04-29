Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

basePath = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = basePath

checkSystem = WshShell.Run("cmd /c cd /d """ & basePath & """ && python -c ""import tkinter, flow_worker.launcher""", 0, True)

If checkSystem = 0 Then
    WshShell.Run "cmd /c cd /d """ & basePath & """ && python -m flow_worker.launcher", 0
Else
    MsgBox "python or tkinter not found for Flow Worker.", vbExclamation, "Flow Worker"
End If

Set fso = Nothing
Set WshShell = Nothing
