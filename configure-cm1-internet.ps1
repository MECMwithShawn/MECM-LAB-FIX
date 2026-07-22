# Script to configure HYD-CM1 Dual-NIC for Domain & Instant Wi-Fi Internet Access
# IMPORTANT: Run inside HYD-CM1 VM as Administrator

Write-Host "--- Configuring HYD-CM1 Internet & Domain Networking ---" -ForegroundColor Cyan

# 1. Configure Ethernet (CorpNet NIC: 10.0.0.7)
$corpNic = Get-NetAdapter | Where-Object { $_.Name -eq 'Ethernet' -or $_.InterfaceAlias -eq 'Ethernet' } | Select-Object -First 1

if ($corpNic) {
    Write-Host "Setting static CorpNet IP on $($corpNic.Name)..." -ForegroundColor Yellow
    Remove-NetIPAddress -InterfaceAlias $corpNic.Name -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
    New-NetIPAddress -InterfaceAlias $corpNic.Name -IPAddress "10.0.0.7" -PrefixLength 24 -ErrorAction SilentlyContinue
    
    # Set Primary DNS to DC1 (10.0.0.1) and Alternate DNS to 8.8.8.8 for instant internet DNS fallback
    Set-DnsClientServerAddress -InterfaceAlias $corpNic.Name -ServerAddresses ("10.0.0.1", "8.8.8.8")
}

# 2. Configure Ethernet 2 (InterNet NIC: 192.168.16.7)
$inetNic = Get-NetAdapter | Where-Object { $_.Name -eq 'Ethernet 2' -or $_.InterfaceAlias -eq 'Ethernet 2' } | Select-Object -First 1

if ($inetNic) {
    Write-Host "Setting static Internet NAT IP on $($inetNic.Name)..." -ForegroundColor Yellow
    Remove-NetIPAddress -InterfaceAlias $inetNic.Name -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
    New-NetIPAddress -InterfaceAlias $inetNic.Name -IPAddress "192.168.16.7" -PrefixLength 24 -DefaultGateway "192.168.16.1" -ErrorAction SilentlyContinue
    Set-DnsClientServerAddress -InterfaceAlias $inetNic.Name -ServerAddresses ("8.8.8.8", "1.1.1.1")
    
    # Set interface metrics so Internet NIC is preferred for 0.0.0.0/0
    Set-NetIPInterface -InterfaceAlias $inetNic.Name -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 10
}

if ($corpNic) {
    Set-NetIPInterface -InterfaceAlias $corpNic.Name -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 500
}

Write-Host "Testing Internet Connectivity..." -ForegroundColor Cyan
try {
    $ping = Test-Connection -ComputerName 8.8.8.8 -Count 1 -Quiet
    $dns = Test-Connection -ComputerName www.google.com -Count 1 -Quiet
    Write-Host "Ping 8.8.8.8: $ping" -ForegroundColor ($ping ? "Green" : "Red")
    Write-Host "DNS google.com: $dns" -ForegroundColor ($dns ? "Green" : "Red")
} catch {
    Write-Error "Test failed: $_"
}

Write-Host "--- Configuration Complete ---" -ForegroundColor Green
