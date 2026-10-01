<#
    preflight.ps1 - FinishLynx <-> RFID middleware connection check.

    Verifies the real wiring rather than the middleware's scoreboard_connected
    flag, which only reports "have I received a packet yet" and reads false
    whenever no event is armed.

    Usage:
      .\preflight.ps1                 Run all checks and report.
      .\preflight.ps1 -Probe          Also inject a synthetic scoreboard packet
                                      to prove the UDP listener end-to-end.
                                      Leaves a fake event in the middleware -
                                      restart it before a real meet.
      .\preflight.ps1 -SaveBaseline   Snapshot Lynx.cfg as known-good (only if
                                      the settings validate).
      .\preflight.ps1 -Restore        Restore the known-good Lynx.cfg.
                                      FinishLynx must be closed.

    Why -SaveBaseline matters: FinishLynx writes Lynx.cfg only on a clean exit.
    A crash, a power loss, or a Task Manager kill reverts every setting to the
    last clean write - which is exactly how this wiring gets lost.
#>
[CmdletBinding()]
param(
    [switch]$Probe,
    [switch]$SaveBaseline,
    [switch]$Restore
)

$LynxCfg   = 'C:\Lynx\Lynx.cfg'
$Baseline  = Join-Path $PSScriptRoot 'Lynx.cfg.known-good'
$StatusUrl = 'http://127.0.0.1:8080/api/status'

$LapTimePort    = 5000   # middleware -> FinishLynx (TCP, FinishLynx listens)
$ScoreboardPort = 5891   # FinishLynx -> middleware (UDP, middleware listens)

$script:Fail = 0
$script:Warn = 0

function Say-Pass { param($m) Write-Host "  [ OK ] $m" -ForegroundColor Green }
function Say-Warn { param($m) Write-Host "  [WARN] $m" -ForegroundColor Yellow; $script:Warn++ }
function Say-Fail { param($m) Write-Host "  [FAIL] $m" -ForegroundColor Red;    $script:Fail++ }
function Say-Info { param($m) Write-Host "  [info] $m" -ForegroundColor DarkGray }
function Section  { param($m) Write-Host "`n$m" -ForegroundColor Cyan }

# Parse Lynx.cfg into key -> value. Key is text before the first ':',
# value is everything after the first '='.
function Read-LynxCfg {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path $Path)) { return $map }
    foreach ($line in (Get-Content $Path)) {
        if ($line -notmatch '^\\') { continue }
        $c = $line.IndexOf(':')
        $e = $line.IndexOf('=')
        if ($c -lt 0 -or $e -lt 0 -or $c -gt $e) { continue }
        $map[$line.Substring(0, $c)] = $line.Substring($e + 1)
    }
    return $map
}

# Find the scoreboard / LapTime device index whose SerialPort targets a port.
function Find-Index {
    param($Cfg, [string]$Prefix, [int]$Port)
    foreach ($k in $Cfg.Keys) {
        if ($k -match "^\\$Prefix\\(\d+)\\SerialPort$") {
            # Capture now: the next -match overwrites $Matches.
            $idx = $Matches[1]
            if ($Cfg[$k] -match ",\s*$Port\s*$") { return $idx }
        }
    }
    return $null
}

# ---------------------------------------------------------------- Restore ---
if ($Restore) {
    Section 'Restore known-good Lynx.cfg'
    if (-not (Test-Path $Baseline)) {
        Say-Fail "No baseline at $Baseline - run -SaveBaseline first."
        exit 1
    }
    if (Get-Process -Name Lynx -ErrorAction SilentlyContinue) {
        Say-Fail 'FinishLynx is running. Close it first or it will overwrite the restore on exit.'
        exit 1
    }
    Copy-Item $LynxCfg "$LynxCfg.before-restore" -Force
    Copy-Item $Baseline $LynxCfg -Force
    Say-Pass "Restored. Previous file kept at $LynxCfg.before-restore"
    exit 0
}

Write-Host "FinishLynx <-> middleware preflight   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor White

# ------------------------------------------------------------- Processes ---
Section 'Processes'
$lynx = Get-Process -Name Lynx -ErrorAction SilentlyContinue
if ($lynx) { Say-Pass "FinishLynx running (PID $($lynx.Id), started $($lynx.StartTime))" }
else       { Say-Fail 'FinishLynx is not running.' }

# ------------------------------------------------------ LapTime TCP 5000 ---
Section "LapTime channel  (middleware -> FinishLynx, TCP $LapTimePort)"
$listen = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
          Where-Object { $_.LocalPort -eq $LapTimePort }
if ($listen) {
    $owner = 'unknown'
    $op = Get-Process -Id $listen[0].OwningProcess -ErrorAction SilentlyContinue
    if ($op) { $owner = $op.ProcessName }
    if ($owner -eq 'Lynx') { Say-Pass "FinishLynx is listening on $LapTimePort." }
    else { Say-Fail "Port $LapTimePort is held by '$owner', not FinishLynx." }
} else {
    Say-Fail "Nothing is listening on $LapTimePort. Set the LapTime device to Network (listen), port $LapTimePort."
}

$estab = Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue |
         Where-Object { $_.LocalPort -eq $LapTimePort -or $_.RemotePort -eq $LapTimePort }
if ($estab) { Say-Pass "Middleware is attached ($($estab.Count) established session(s))." }
else        { Say-Warn "No established session on $LapTimePort yet - the middleware retries every ~5s." }

# --------------------------------------------------- Scoreboard UDP 5891 ---
Section "Scoreboard channel  (FinishLynx -> middleware, UDP $ScoreboardPort)"
$udp = Get-NetUDPEndpoint -ErrorAction SilentlyContinue |
       Where-Object { $_.LocalPort -eq $ScoreboardPort }
if ($udp) {
    $owner = 'unknown'
    $op = Get-Process -Id $udp[0].OwningProcess -ErrorAction SilentlyContinue
    if ($op) { $owner = $op.ProcessName }
    Say-Pass "Middleware listener bound on UDP $ScoreboardPort (held by '$owner')."
} else {
    Say-Fail "Nothing is bound to UDP $ScoreboardPort - the middleware listener is not up."
}
Say-Info 'UDP is connectionless: FinishLynx only emits when an event is armed or a clock runs.'

# ------------------------------------------------------- Middleware API ---
Section 'Middleware status API'
$status = $null
try { $status = Invoke-RestMethod -Uri $StatusUrl -TimeoutSec 8 } catch { }
if ($status) {
    Say-Pass 'Console API reachable on 8080.'

    if ($status.finishlynx_connected) { Say-Pass 'finishlynx_connected = true (LapTime link up).' }
    else { Say-Fail 'finishlynx_connected = false - middleware cannot reach the LapTime port.' }

    if ($status.scoreboard_listener_active) { Say-Pass 'scoreboard_listener_active = true.' }
    else { Say-Fail 'scoreboard_listener_active = false - listener thread is not running.' }

    if ($status.reader_connected) { Say-Pass 'reader_connected = true.' }
    else { Say-Warn 'reader_connected = false - RFID reader is not attached.' }

    if ($status.scoreboard_connected) {
        Say-Info "scoreboard_connected = true (last seen event $($status.scoreboard_event), heat $($status.scoreboard_heat))."
    } else {
        Say-Info 'scoreboard_connected = false - expected until FinishLynx first emits. Not a fault on its own.'
    }
} else {
    Say-Fail 'Middleware console API not answering on 8080 - is the middleware running?'
}

# ------------------------------------------------------ Lynx.cfg settings ---
Section 'Lynx.cfg settings'
if (Test-Path $LynxCfg) {
    $cfgTime = (Get-Item $LynxCfg).LastWriteTime
    if ($lynx -and $cfgTime -lt $lynx.StartTime) {
        Say-Warn "Lynx.cfg was last written $cfgTime, before the current FinishLynx session started."
        Say-Info 'FinishLynx saves settings only on exit, so changes made this session are not on disk yet.'
    }

    $cfg = Read-LynxCfg $LynxCfg

    $sb = Find-Index $cfg 'Scoreboards' $ScoreboardPort
    if ($sb) {
        $name = $cfg["\Scoreboards\$sb\Name"]
        Say-Pass "Scoreboard $sb ('$name') targets port $ScoreboardPort."

        $port = $cfg["\Scoreboards\$sb\SerialPort"]
        if ($port -match '^-3,') { Say-Pass "  transport = UDP ($port)" }
        else { Say-Fail "  transport should be Network (UDP); found '$port'" }

        if ($cfg["\Scoreboards\$sb\ResultsMode"] -ne '0') { Say-Pass '  Results = on (carries event/round/heat)' }
        else { Say-Fail '  Results = Off. Set it to Normal - this is the only channel carrying event/round/heat.' }

        if ($cfg["\Scoreboards\$sb\ResultsIfArmed"] -eq '1') { Say-Pass '  Results if armed = on' }
        else { Say-Warn '  Results if armed = off. Turn on, or event/heat only arrives once results exist.' }

        if ($cfg["\Scoreboards\$sb\TimeMode"] -ne '0') { Say-Pass '  Running Time = on (auto-fires the gun)' }
        else { Say-Warn '  Running Time = Off. Turn on for automatic gun detection.' }

        $script = $cfg["\Scoreboards\$sb\Script"]
        if ($script -match '^Example2?\.lss$') { Say-Pass "  Script = $script" }
        else { Say-Fail "  Script = '$script'. The parser expects Example.lss or Example2.lss." }
    } else {
        Say-Fail "No scoreboard entry targets port $ScoreboardPort."
    }

    $lt = Find-Index $cfg 'LapTime' $LapTimePort
    if ($lt) {
        $name = $cfg["\LapTime\$lt\Name"]
        Say-Pass "LapTime device $lt ('$name') targets port $LapTimePort."

        $port = $cfg["\LapTime\$lt\SerialPort"]
        if ($port -match '^-2,') { Say-Pass "  transport = Network listen ($port)" }
        else { Say-Fail "  transport should be Network (listen); found '$port'. The middleware dials in." }

        # Device dropdown -> Lynx.cfg Type. Confirmed live 2026-09-12: with
        # Device = "Lynx" (Type 11) the middleware's S/T packets land in the
        # event; with the Ipico entry (Type 9) nothing parses.
        if ($cfg["\LapTime\$lt\Type"] -eq '11') { Say-Pass '  Device = Lynx (Type 11, generic Lynx LapTime protocol)' }
        else { Say-Fail "  Type = $($cfg["\LapTime\$lt\Type"]) - Device must be 'Lynx' (Type 11). Type 9 is the Ipico protocol and will not parse." }
    } else {
        Say-Fail "No LapTime device targets port $LapTimePort."
    }
} else {
    Say-Fail "$LynxCfg not found."
}

# ------------------------------------------------------------------ Probe ---
if ($Probe) {
    Section 'Live listener probe'
    if (-not $status) {
        Say-Warn 'Skipped - status API unavailable, cannot read the result.'
    } else {
        $magicEvent = 4242
        $payload = "*** Start List Header ***`r`n" +
                   "Event name         : PREFLIGHT PROBE`r`n" +
                   "Event number       : $magicEvent`r`n" +
                   "Round number       : 1`r`n" +
                   "Heat number        : 9`r`n" +
                   "*** Start List Trailer ***`r`n"
        $bytes = [Text.Encoding]::ASCII.GetBytes($payload)
        $client = New-Object Net.Sockets.UdpClient
        $client.Send($bytes, $bytes.Length, '127.0.0.1', $ScoreboardPort) | Out-Null
        $client.Close()
        Start-Sleep -Seconds 2

        $after = $null
        try { $after = Invoke-RestMethod -Uri $StatusUrl -TimeoutSec 8 } catch { }
        if ($after -and $after.scoreboard_event -eq $magicEvent) {
            Say-Pass 'Listener received and parsed the probe packet - the middleware end is healthy.'
            Say-Warn "Synthetic event $magicEvent is now in middleware state. Restart the middleware before a real meet."
        } else {
            Say-Fail 'Probe sent but the middleware did not register it.'
        }
    }
}

# ---------------------------------------------------------- SaveBaseline ---
if ($SaveBaseline) {
    Section 'Save known-good Lynx.cfg'
    if ($script:Fail -gt 0) {
        Say-Fail "Refusing to snapshot - $($script:Fail) check(s) failed. Fix them first."
    } elseif ($lynx) {
        Say-Fail 'FinishLynx is running, so Lynx.cfg is stale. Close FinishLynx, then re-run -SaveBaseline.'
    } else {
        Copy-Item $LynxCfg $Baseline -Force
        Say-Pass "Baseline saved to $Baseline"
        Say-Info 'Recover it later with:  .\preflight.ps1 -Restore   (FinishLynx closed)'
    }
}

# ---------------------------------------------------------------- Summary ---
Section 'Summary'
if ($script:Fail -eq 0 -and $script:Warn -eq 0) {
    Write-Host '  All checks passed.' -ForegroundColor Green
} else {
    Write-Host "  $($script:Fail) failed, $($script:Warn) warning(s)." -ForegroundColor $(if ($script:Fail -gt 0) { 'Red' } else { 'Yellow' })
}
Write-Host ''
if ($script:Fail -gt 0) { exit 1 }
exit 0
