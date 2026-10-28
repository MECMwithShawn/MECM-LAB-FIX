# Script to configure static IP and trigger captive portal for wifi-xguest
# Use this when DHCP on wifi-xguest is failing / down.
# IMPORTANT: Run from Elevated PowerShell (Admin)

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning "Please run this script as Administrator."
    Pause
    exit
}

# Auto-detect active Wi-Fi adapter
$Adapters = Get-NetAdapter -Name "Wi-Fi*" | Where-Object { $_.Status -eq 'Up' }
if (-not $Adapters) {
    $Adapters = Get-NetAdapter -Name "Wi-Fi*"
    if (-not $Adapters) {
        Write-Host "[-] Could not find any Wi-Fi adapter on this system." -ForegroundColor Red
        Pause
        exit
    }
}

$Adapter = $Adapters[0]
$InterfaceAlias = $Adapter.Name

# Verified working configuration on wifi-xguest
$targetIP = "100.88.63.244"
$targetGW = "100.88.62.1"
$prefixLength = 23 # Subnet Mask: 255.255.254.0
$dnsServers = @("172.64.36.1", "172.64.36.2")

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Applying Verified Static IP for wifi-xguest" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  Adapter:         $InterfaceAlias"
Write-Host "  IP Address:      $targetIP"
Write-Host "  Subnet Mask:     255.255.254.0 (/$prefixLength)"
Write-Host "  Default Gateway: $targetGW"
Write-Host "  DNS Resolvers:   $($dnsServers -join ', ')"
Write-Host ""

# 1. Disable DHCP & clear any existing manual IPv4 assignments
Set-NetIPInterface -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 -Dhcp Disabled -ErrorAction SilentlyContinue
Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object PrefixOrigin -eq 'Manual' | Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue

# 2. Assign static IP, subnet, and default gateway
New-NetIPAddress -InterfaceAlias $InterfaceAlias -IPAddress $targetIP -PrefixLength $prefixLength -DefaultGateway $targetGW -AddressFamily IPv4 -ErrorAction Stop | Out-Null

# 3. Configure DNS
Set-DnsClientServerAddress -InterfaceAlias $InterfaceAlias -ServerAddresses $dnsServers -ErrorAction SilentlyContinue

Write-Host "[+] Static IP successfully applied!" -ForegroundColor Green

# 4. Trigger the captive portal in browser
Write-Host "[*] Launching browser to trigger captive portal..." -ForegroundColor Cyan
Start-Process "http://neverssl.com"
Start-Process "http://msftconnecttest.com/redirect"
Start-Process "http://1.1.1.1"

Write-Host "`nAll set! Check your browser for the guest login page." -ForegroundColor Green
Pause
