$ErrorActionPreference = "Stop"

Write-Host "Removing corp NIC default route (active + persistent)..." -ForegroundColor Cyan
Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -InterfaceAlias "Ethernet" -ErrorAction SilentlyContinue |
    Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue

Get-NetRoute -PolicyStore PersistentStore -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -InterfaceAlias "Ethernet" -ErrorAction SilentlyContinue |
    Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "Pinning interface metrics (internet preferred)..." -ForegroundColor Cyan
Set-NetIPInterface -InterfaceAlias "Ethernet 2" -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 10
Set-NetIPInterface -InterfaceAlias "Ethernet"   -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 500

Write-Host ""
Write-Host "Resulting default routes:" -ForegroundColor Green
Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" |
    Sort-Object InterfaceAlias |
    Format-Table ifIndex, InterfaceAlias, NextHop, RouteMetric, Store -AutoSize

Write-Host ""
Write-Host "Resulting interface metrics:" -ForegroundColor Green
Get-NetIPInterface -AddressFamily IPv4 |
    Sort-Object InterfaceMetric |
    Format-Table ifIndex, InterfaceAlias, AutomaticMetric, InterfaceMetric -AutoSize

Write-Host ""
Write-Host "Connectivity check (first 4 hops to 8.8.8.8):" -ForegroundColor Green
tracert -d -h 4 8.8.8.8
