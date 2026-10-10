<#
.SYNOPSIS
    Pin a Windows timing PC to its static address so it can always see the
    LoRa gateway and the RFID reader.

.DESCRIPTION
    A station's PC and its gateway sit alone on a small wired network with no
    DHCP server. Left on "obtain an address automatically" Windows waits for a
    DHCP reply, never gets one, and falls back to a 169.254.x.x address - at
    which point the gateway is unreachable even though the cable is fine.
    This is the usual reason a station works on one machine and not another.

    Run once per PC, as Administrator.

.EXAMPLE
    .\setup-windows-station.ps1
    .\setup-windows-station.ps1 -Address 192.168.0.98 -Adapter "Ethernet 2"
#>
[CmdletBinding()]
param(
    [string]$Adapter,
    [string]$Address = "192.168.0.97",
    [string]$Mask    = "255.255.255.0",
    [string]$Gateway = "192.168.0.60",
    [switch]$Revert
)

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an Administrator PowerShell prompt."
}

if (-not $Adapter) {
    $wired = Get-NetAdapter -Physical | Where-Object { $_.Status -eq "Up" -and $_.MediaType -ne "802.11" }
    if ($wired.Count -eq 1) {
        $Adapter = $wired[0].Name
    } else {
        Write-Host "Wired adapters:"
        Get-NetAdapter -Physical | Format-Table Name, Status, LinkSpeed -AutoSize
        throw "Could not pick an adapter automatically. Re-run with -Adapter '<name>'."
    }
}
Write-Host "Adapter: $Adapter"

if ($Revert) {
    netsh interface ip set address name="$Adapter" source=dhcp | Out-Null
    Write-Host "$Adapter set back to DHCP."
    exit 0
}

# A static address means no waiting on a DHCP server that will never answer.
netsh interface ip set address name="$Adapter" static $Address $Mask | Out-Null
Write-Host "$Adapter pinned to $Address/$Mask"

# Timing traffic is local; a default route here would only break internet access.
netsh interface ip set dns name="$Adapter" static none | Out-Null

Start-Sleep -Seconds 3

Write-Host "`nChecking the gateway at $Gateway ..."
if (Test-Connection -ComputerName $Gateway -Count 2 -Quiet) {
    Write-Host "  reachable"
    if ((Test-NetConnection -ComputerName $Gateway -Port 8886 -WarningAction SilentlyContinue).TcpTestSucceeded) {
        Write-Host "  radio data port 8886 open"
        Write-Host "`nStation ready. Set lora.port to ${Gateway}:8886 in config.yaml."
    } else {
        Write-Warning "Gateway answers but data port 8886 is closed - check its Socket A settings."
    }
} else {
    Write-Warning "No reply from $Gateway."
    Write-Host "Check the cable, then run:  python setup_gateway.py discover"
    Write-Host "If the gateway holds a factory address, adopt it:  python setup_gateway.py adopt"
}
