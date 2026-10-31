@echo off
rem Double-click launcher for the AirPlayPC UI - no console window.
rem
rem Prefer "AirPlayPC.vbs": a .cmd unavoidably flashes its own console when
rem double-clicked, and powershell's console exists for a beat before
rem -WindowStyle Hidden takes effect ("start /min" below keeps that beat off
rem the foreground, but it still blips the taskbar). The .vbs starts the same
rem guarded command with the console hidden from birth - zero flashes. This
rem file stays because .cmd works even where Windows Script Host is disabled.
rem
rem The console is hidden, which used to mean that everything airplay-ui.ps1 does
rem before its window appears was invisible: a XAML parse error, a failed
rem dot-source, or a plain "exit 1" all produced literally nothing, and "start"
rem threw the exit code away on top of that. A hidden process cannot be given a
rem console back - with -File, powershell exits on the throw and the window would
rem close before anything could be read - so the guard lives here instead. We run
rem the script under -Command, catch a terminating error AND check its exit code,
rem and report either one in the only UI a hidden process has: a MessageBox. If
rem even that cannot load, the same text is left in %TEMP%\pcairplay-crash.log.
rem The success path is unchanged: no console, and this window closes at once.
rem
rem The script path travels in an environment variable rather than being pasted
rem into the -Command string. Inlined as '%~dp0airplay-ui.ps1', a repo checked
rem out under a path containing an apostrophe (C:\Users\O'Brien\...) closes the
rem PowerShell string early, so the whole command - guard included - fails to
rem parse and nothing happens at all. Verified: the inlined form ran nothing and
rem showed nothing from such a path. "set" quotes the value, so & and spaces in
rem the path are safe too.
set "PCAIRPLAY_UI=%~dp0airplay-ui.ps1"
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$ErrorActionPreference='Continue'; try { Add-Type -AssemblyName PresentationFramework; & $env:PCAIRPLAY_UI; if ($LASTEXITCODE) { throw ('airplay-ui.ps1 exited with code ' + $LASTEXITCODE + '.') } } catch { $m = 'AirPlayPC failed to start.' + [Environment]::NewLine + [Environment]::NewLine + $_.Exception.Message; try { Set-Content -LiteralPath ($env:TEMP + '\pcairplay-crash.log') -Value ($m + [Environment]::NewLine + $_.ScriptStackTrace) } catch { }; try { [void][System.Windows.MessageBox]::Show($m, 'AirPlayPC', 'OK', 'Error') } catch { }; exit 1 }"
