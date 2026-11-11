' Launch daily pipeline hidden. Scheduler should pass the bat path.
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

If WScript.Arguments.Count >= 1 Then
    bat = WScript.Arguments(0)
Else
    bat = scriptDir & "\scripts_run_daily.bat"
End If

WshShell.Run """" & bat & """ /scheduled", 0, False
Set WshShell = Nothing
Set fso = Nothing
