# Script to revert Wi-Fi adapter back to dynamic DHCP and clear static settings
# IMPORTANT: Run from Elevated PowerShell (Admin)

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning "Please run this script as Administrator."
    Pause
    exit
}

$Adapters = Get-NetAdapter -Name "Wi-Fi*" 
if (-not $Adapters) {
    Write-Host "Could not find any Wi-Fi adapters." -ForegroundColor Red
    Pause
    exit
}

foreach ($Adapter in $Adapters) {
    $InterfaceAlias = $Adapter.Name
    Write-Host "Reverting $($InterfaceAlias) to DHCP..." -ForegroundColor Cyan
    
    # Enable DHCP
    Set-NetIPInterface -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 -Dhcp Enabled -ErrorAction SilentlyContinue
    
    # Remove manual IPs
    Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object PrefixOrigin -eq 'Manual' | Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    
    # Reset DNS
    Set-DnsClientServerAddress -InterfaceAlias $InterfaceAlias -ResetServerAddresses -ErrorAction SilentlyContinue
}

Write-Host "`n[+] Successfully reverted Wi-Fi adapters back to automatic IP (DHCP)!" -ForegroundColor Green
Pause
