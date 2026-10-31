@echo off
rem Double-click launcher for the framed (iPhone-chassis) mirror view.
rem Same guarded-hidden-console pattern as "AirPlayPC.cmd" - see the comments
rem there for why the guard and the env-var path handoff both exist.
rem
rem Rarely needed by hand any more: the UI opens and closes the frame itself
rem (the "iPhone frame" switch). This stays for running the frame standalone
rem next to a start-airplay.ps1 console session.
set "PCAIRPLAY_FRAME=%~dp0frame-mirror.ps1"
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$ErrorActionPreference='Continue'; try { Add-Type -AssemblyName PresentationFramework; & $env:PCAIRPLAY_FRAME; if ($LASTEXITCODE) { throw ('frame-mirror.ps1 exited with code ' + $LASTEXITCODE + '.') } } catch { $m = 'The framed mirror failed to start.' + [Environment]::NewLine + [Environment]::NewLine + $_.Exception.Message; try { Set-Content -LiteralPath ($env:TEMP + '\pcairplay-crash.log') -Value ($m + [Environment]::NewLine + $_.ScriptStackTrace) } catch { }; try { [void][System.Windows.MessageBox]::Show($m, 'AirPlayPC', 'OK', 'Error') } catch { }; exit 1 }"
