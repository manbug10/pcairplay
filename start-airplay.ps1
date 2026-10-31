<#
.SYNOPSIS
    Starts the AirPlay receiver, tuned for low-latency demo / presentation use.
.DESCRIPTION
    Locates the UxPlay engine, sets up its GStreamer environment, and launches it
    with settings chosen for responsiveness rather than perfect A/V sync.

    Once running, on the iPhone: Control Center -> Screen Mirroring -> pick this PC.

    Every flag emitted here was checked against "uxplay -h" for 1.72.1-3 and
    parse-tested against the installed engine. Only the 1.x build has a command
    line at all - uxplay-windows 2.x is a Qt6 app with no uxplay.exe, and this
    script refuses to guess at it.
.PARAMETER Name
    The name shown in the iPhone's Screen Mirroring list. Default: "PC".
    Double quotes and trailing backslashes are stripped - they corrupt the
    native command line and silently swallow the flag that follows.
.PARAMETER Fps
    Cap on the streaming framerate ("-fps"). UxPlay's own default is only 30; we
    default to 60 for smoother demos. Drop back to 30 if the network is congested
    and you see stutter. This caps the stream only - it does not change the
    display mode requested from the phone (see -RefreshRate).
.PARAMETER Resolution
    Display resolution requested from the iPhone, e.g. 1920x1080. Default
    1920x1080. uxplay validates this itself (max 9999x9999) and reports a clear
    error before doing anything, so nothing is re-validated here.
.PARAMETER RefreshRate
    Display refresh rate requested from the iPhone, the "@r" half of uxplay's
    "-s wxh[@r]". Default 60. This used to be wired to -Fps, which meant
    "-Fps 30" also asked the phone for a 30 Hz display mode - a different request
    from "cap the stream at 30 fps".
.PARAMETER Sync
    Enable audio/video timestamp sync (UxPlay's default). Gives correct lip-sync
    but adds buffering latency. Off by default here - for app demos, responsive
    touch feedback matters more than perfect sync. Turn on for video playback.
.PARAMETER Fullscreen
    Start the mirror window fullscreen.
.PARAMETER Pin
    Require a 4-digit PIN on the phone before mirroring is allowed. Also passes
    "-reg", so a phone that has already registered is not re-prompted on every
    reconnect.
.PARAMETER SoftwareDecode
    Force software H.264 decoding. Use only if hardware decode shows artifacts.
.PARAMETER ShareSafe
    Use the OpenGL video sink instead of Direct3D 11. Use this when the mirror
    window itself is being screen-shared into a Teams / Google Meet / Zoom call
    and the other participants see a black rectangle where the iPhone should be.
    The D3D11 sink can present through a hardware overlay, which some window
    capture paths cannot read; the OpenGL sink draws into the window normally.
    Costs hardware presentation, so only reach for it if you actually see black.
    Sharing the WHOLE SCREEN rather than the single window usually avoids the
    problem without this flag.
.PARAMETER NoAudio
    Video only - don't stream audio from the phone.
.PARAMETER Port
    Bind to fixed TCP+UDP ports n, n+1, n+2 (uxplay's "-p n") instead of letting
    the OS choose. This is NOT the fix for a competing receiver: uxplay does not
    bind 7000 by default at all - it takes an ephemeral port and publishes it in
    the mDNS record, so a rival receiver's real damage is its duplicate mDNS
    entry, not the port. Use this only when a third-party firewall ignores
    program rules and has to be given a fixed port to allow.
.PARAMETER LegacyPorts
    Use uxplay's legacy fixed ports - bare "-p" - which are TCP 7000/7001/7100
    and UDP 6000/6001/7011. That is the set setup.ps1's fixed-port firewall rules
    were written for, so this, not -Port, is what makes those rules the operative
    ones. (setup.ps1's UDP rule covers 6000-6009 and does not include 7011.)
.PARAMETER EngineDebug
    Pass uxplay's own "-d 1" (verbose logging, packet data skipped). Combined
    with the default log this shows the exact RTSP request where a session
    stops - written for the open "RTSP holds, no video" stall issue. Expect a
    much bigger log.
.PARAMETER Log
    Accepted for compatibility and now the default - see -NoLog. Passing it
    explicitly does nothing.
.PARAMETER NoLog
    Do NOT tee the engine's console output to %LOCALAPPDATA%\pcairplay\.

    Logging is ON by default. It used to be opt-in, on the grounds that it
    changes the shape of the launch - output goes through a pipe rather than
    straight to the console, so colour is lost and PowerShell reformats the
    engine's stderr. That trade is wrong for this project: when mirroring fails
    it fails silently and after the fact, and an opt-in log is never on for the
    run that actually needed it. Verified to still reach "Initialized server
    socket(s)" with the pipe in place - piping is not the same as the no-console
    launch that stalls before GStreamer initialises.

    Use -NoLog when you want the engine's raw, coloured output in the console.
.PARAMETER DryRun
    Print the engine path and the exact argument vector that would be passed,
    then exit without starting anything. Use it to check argument construction.
.EXAMPLE
    .\start-airplay.ps1
.EXAMPLE
    .\start-airplay.ps1 -Name "Demo Screen" -Fullscreen
.EXAMPLE
    # Watching video, where lip-sync matters more than latency:
    .\start-airplay.ps1 -Sync
.EXAMPLE
    # See what would be launched, without launching it:
    .\start-airplay.ps1 -Pin -DryRun
#>
[CmdletBinding()]
param(
    [string]$Name = 'PC',
    [ValidateRange(15, 120)]
    [int]$Fps = 60,
    [ValidatePattern('^\d+x\d+$')]
    [string]$Resolution = '1920x1080',
    [ValidateRange(1, 255)]
    [int]$RefreshRate = 60,
    [switch]$Sync,
    [switch]$Fullscreen,
    [switch]$Pin,
    [switch]$SoftwareDecode,
    [switch]$ShareSafe,
    [switch]$NoAudio,
    [ValidateRange(1024, 65530)]
    [int]$Port,
    [switch]$LegacyPorts,
    [switch]$EngineDebug,
    [switch]$Log,
    [switch]$NoLog,
    [switch]$DryRun
)

# Logging is the default now; -Log is kept only so existing habits and shortcuts
# do not break. -NoLog is the opt-out.
$Log = -not $NoLog

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'uxplay-common.ps1')

$resolved = Resolve-UxPlayDeviceName -Name $Name
if ($resolved.Changed) {
    Write-Host "Note: -Name adjusted to '$($resolved.Name)' - quotes and trailing backslashes cannot be passed through." -ForegroundColor Yellow
}
if ($resolved.Error) {
    Write-Host ($resolved.Error -replace 'The device name', '-Name') -ForegroundColor Red
    exit 1
}
$Name = $resolved.Name

$ux = Find-UxPlay
if (-not $ux) {
    if ($DryRun) {
        # A dry run exists precisely to preview the argv without an engine
        # (that is how CI exercises it on a box with no UxPlay installed).
        Write-Host "NOTE: UxPlay is not installed - previewing the arguments anyway." -ForegroundColor Yellow
    } else {
        Write-Host "UxPlay is not installed." -ForegroundColor Red
        Write-Host "Run setup.ps1 as Administrator first." -ForegroundColor Yellow
        exit 1
    }
}
# 2.x is installed but has no command line, so every flag below is meaningless
# there. Say so once, in the shared wording, instead of failing obscurely.
if ($ux -and $ux.Kind -ne 'Cli') {
    if ($DryRun) {
        Write-Host "NOTE: the installed build is 2.x (GUI only) - previewing the arguments anyway." -ForegroundColor Yellow
    } else {
        Write-Host (Get-UxPlayIncompatibleMessage -UxPlay $ux) -ForegroundColor Yellow
        exit 1
    }
}
$exe = if ($ux) { $ux.Exe } else { $null }
$pluginDir = if ($ux) { $ux.PluginDir } else { $null }

if ($ux) { Initialize-UxPlayEnvironment -UxPlay $ux }
if ($ux -and -not $pluginDir) {
    Write-Host "WARNING: no GStreamer plugin directory found near $exe." -ForegroundColor Yellow
    Write-Host "         The receiver may start but show a black window (no video sink)." -ForegroundColor Yellow
}

# Registration goes through Bonjour, and a RUNNING Bonjour can still be deaf:
# if another app owns the network-facing UDP 5353 sockets (the Apple Devices
# helpers did on 2026-07-20), the engine registers successfully into a responder
# nobody can hear. The engine will start and look perfect either way, so warn
# now - this is the "Wi-Fi doesn't work but the USB cable does" failure.
$bonjourSock = Get-BonjourSocketState
if ($bonjourSock.State -ne 'Listening') {
    $why = switch ($bonjourSock.State) {
        'Deaf'      { 'running but has NO network mDNS socket' }
        'Stopped'   { 'installed but not running' }
        'NoService' { 'not installed' }
    }
    Write-Host "WARNING: Bonjour is $why - iPhones will NOT see this PC." -ForegroundColor Yellow
    if ($bonjourSock.State -eq 'Deaf' -and $bonjourSock.Holders.Count -gt 0) {
        Write-Host "         UDP 5353 is held by: $($bonjourSock.Holders -join ', ')" -ForegroundColor Yellow
    }
    Write-Host "         Run .\doctor.ps1 - check 2 prints the exact fix." -ForegroundColor Yellow
}

# A second receiver is not just a port clash: it advertises over mDNS too, so the
# phone lists two entries and tapping the dead one looks exactly like a protocol
# failure. A leftover uxplay.exe from an earlier run does the same thing but has
# no window, so include it here - the UI already does.
$rivals = @(Get-CompetingReceiverProcess) + @(Get-UxPlayEngineProcess)
if ($rivals.Count -gt 0) {
    $names = @($rivals | Select-Object -ExpandProperty ProcessName -Unique)
    Write-Host "WARNING: another AirPlay receiver (or a leftover engine from an earlier" -ForegroundColor Yellow
    Write-Host "         run) is already running: $($names -join ', ')" -ForegroundColor Yellow
    Write-Host "         It also advertises over mDNS, so the iPhone will list two entries." -ForegroundColor Yellow
    Write-Host "         Close it BEFORE starting this one:" -ForegroundColor Yellow
    # Every distinct name, quoted: closing only the first leaves the second still
    # advertising, and Reflector ships as e.g. "Reflector 4", which will not parse
    # unquoted.
    $quoted = ($names | ForEach-Object { "'$_'" }) -join ','
    Write-Host "           Stop-Process -Name $quoted -Force" -ForegroundColor Gray
}

# --- Build the argument list ---------------------------------------------
# Shared with airplay-ui.ps1 - see Build-UxPlayArgs in uxplay-common.ps1. The two
# entry points MUST NOT construct this independently again; they drifted last
# time, and the drift was invisible from either side.
$built = Build-UxPlayArgs -Name $Name -Resolution $Resolution -RefreshRate $RefreshRate `
    -Fps $Fps -PluginDir $pluginDir -Sync:$Sync -Fullscreen:$Fullscreen -Pin:$Pin `
    -SoftwareDecode:$SoftwareDecode -ShareSafe:$ShareSafe -NoAudio:$NoAudio `
    -Port $Port -LegacyPorts:$LegacyPorts -EngineDebug:$EngineDebug
$argList = $built.Args
foreach ($n in $built.Notes) { Write-Host "Note: $n" -ForegroundColor Yellow }

if ($DryRun) {
    Write-Host ""
    Write-Host "  DRY RUN - nothing will be started." -ForegroundColor Cyan
    Write-Host "  Engine : $(if ($exe) { $exe } else { '(not installed - these are the args that would be used)' })"
    Write-Host "  Args   :"
    foreach ($a in $argList) { Write-Host "    $a" -ForegroundColor Gray }
    Write-Host ""
    exit 0
}

# --- Report ---------------------------------------------------------------
$ip = Get-LanIPAddress

Write-Host ""
Write-Host "  Starting the AirPlay receiver" -ForegroundColor Cyan
Write-Host "  ------------------------------------------" -ForegroundColor DarkGray
Write-Host "  Appears on iPhone as : $Name"
Write-Host "  This PC              : $ip"
Write-Host "  Video                : $Resolution @ ${RefreshRate}Hz, capped at ${Fps}fps"
Write-Host "  Mode                 : $(if ($Sync) { 'A/V synced (video playback)' } else { 'Low latency (demos)' })"
Write-Host "  ------------------------------------------" -ForegroundColor DarkGray

# --- Optional log ---------------------------------------------------------
$logPath = $null
if ($Log) {
    $logPath = New-PCAirPlayLogPath -Prefix 'start-airplay'
    # The header goes through Tee-Object as well, deliberately: PS 5.1's
    # Tee-Object has no -Encoding, and writing the header with Set-Content left
    # the engine's output appended in a different encoding - the file read back
    # as "u x p l a y . e x e". Letting Tee create the file makes both halves
    # agree by construction, whatever it picks.
    @(
        "engine  : $exe"
        "version : $($ux.Version)"
        "plugins : $pluginDir"
        "lan ip  : $ip"
        "argv    : $($argList -join ' ')"
        "started : $(Get-Date -Format 's')"
        '---'
    ) | Tee-Object -FilePath $logPath | Out-Null
    Write-Host "  Logging to $logPath" -ForegroundColor DarkGray
}

Write-Host "  Once it reports 'Initialized server socket(s)', on the iPhone:" -ForegroundColor Green
Write-Host "  Control Center -> Screen Mirroring -> $Name" -ForegroundColor Green
Write-Host "  Press Ctrl+C here to stop." -ForegroundColor DarkGray
Write-Host ""

Write-Verbose "Engine : $exe"
Write-Verbose "Args   : $($argList -join ' ')"

# --- Launch ---------------------------------------------------------------
# Foreground and attached to this console on purpose: launched without one (a
# background job, for instance) the engine prints its banner and then stalls
# before GStreamer initialises, with no error and no mDNS registration.
$startedAt = Get-Date
$code = 0
try {
    if ($logPath) {
        # 2>&1 is what makes the engine's stderr reachable by Tee-Object, but
        # under $ErrorActionPreference='Stop' the first stderr line becomes a
        # terminating NativeCommandError - and UxPlay emits the benign "Failed to
        # load plugin 'libgstcurl.dll'" during startup, which would kill the
        # receiver on every logged run. Verified. Scope the preference to the
        # pipeline and restore it immediately.
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $exe @argList 2>&1 | Tee-Object -FilePath $logPath -Append }
        finally { $ErrorActionPreference = $prev }
    } else {
        & $exe @argList
    }
    $code = $LASTEXITCODE
}
finally {
    # Ctrl+C reaches the engine as well (it shares this console's process group),
    # so this is normally a no-op. It matters when the script is torn down first:
    # a surviving engine keeps its mDNS registration alive, and the phone then
    # lists a receiver that nothing is driving. Matched by parent PID so a second
    # session's engine is never touched. Best-effort - a hard console close can
    # end powershell.exe before any finally block runs.
    try {
        Get-CimInstance Win32_Process -Filter "Name = 'uxplay.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ParentProcessId -eq $PID } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch { }
}

if ($logPath) { Write-Host "  Log: $logPath" -ForegroundColor DarkGray }

# $ErrorActionPreference does not apply to native exit codes, so without this the
# script reports success no matter how the engine ended. A non-zero code after a
# real session is normal (Ctrl+C, window closed); only an immediate exit means it
# never started, so diagnose on elapsed time rather than on the code alone.
if ($code -ne 0) {
    Write-Host ""
    if (((Get-Date) - $startedAt).TotalSeconds -lt 3) {
        Write-Host "  The receiver exited immediately (code $code) - it did not start." -ForegroundColor Red
        Write-Host "  The error above says why. Run .\doctor.ps1 for the usual causes." -ForegroundColor Yellow
    } else {
        Write-Host "  Receiver stopped (exit code $code)." -ForegroundColor DarkGray
    }
}
exit $code
