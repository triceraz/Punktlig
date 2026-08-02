' Launch a batch file with no console window.
'
' The site task has to keep its interactive logon type, because pushing to
' GitHub reads credentials from the user's own credential store. That logon
' type is also what makes cmd.exe flash a console onto the desktop every ten
' minutes. Running the batch through WScript.Shell with window style 0 keeps
' the security context exactly as it was and simply never shows the window.
'
' Usage: wscript.exe run-hidden.vbs "C:\path\to\script.cmd"

Set shell = CreateObject("WScript.Shell")
If WScript.Arguments.Count = 0 Then
    WScript.Quit 2
End If
' Wait for completion so the scheduler still sees the real exit code.
WScript.Quit shell.Run("""" & WScript.Arguments(0) & """", 0, True)
