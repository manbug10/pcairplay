' Zero-flash double-click launcher for the AirPlayPC UI.
'
' "AirPlayPC.cmd" still works, but a .cmd cannot avoid flashing its own
' console window when double-clicked, plus a second brief powershell console
' on top ("-WindowStyle Hidden" is honoured only after powershell has already
' opened one). WScript.Shell.Run with window style 0 creates the powershell
' console HIDDEN from birth, so this path shows no window at all - which is
' the point of the whole app now: nothing of its machinery is ever visible.
'
' Same guard as the .cmd (see the comments there for why each piece exists):
' the script path travels in an environment variable so an apostrophe in the
' checkout path cannot break the inner PowerShell string; a failure to start
' surfaces in a MessageBox, and the same text lands in
' %TEMP%\pcairplay-crash.log in case even WPF cannot load.
Option Explicit
Dim sh, fso, env, cmd
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set env = sh.Environment("PROCESS")
env("PCAIRPLAY_UI") = fso.GetParentFolderName(WScript.ScriptFullName) & "\airplay-ui.ps1"
cmd = "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command " & _
      """$ErrorActionPreference='Continue'; try { Add-Type -AssemblyName PresentationFramework; & $env:PCAIRPLAY_UI; if ($LASTEXITCODE) { throw ('airplay-ui.ps1 exited with code ' + $LASTEXITCODE + '.') } } catch { $m = 'AirPlayPC failed to start.' + [Environment]::NewLine + [Environment]::NewLine + $_.Exception.Message; try { Set-Content -LiteralPath ($env:TEMP + '\pcairplay-crash.log') -Value ($m + [Environment]::NewLine + $_.ScriptStackTrace) } catch { }; try { [void][System.Windows.MessageBox]::Show($m, 'AirPlayPC', 'OK', 'Error') } catch { }; exit 1 }"""
sh.Run cmd, 0, False
