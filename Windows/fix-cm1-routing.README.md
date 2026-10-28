# fix-cm1-routing.ps1 - Breakdown

## Purpose
This script fixes slow or inconsistent internet access on a dual-NIC Windows VM by enforcing a clean routing preference:

- `Ethernet 2` = internet path (preferred)
- `Ethernet` = private/corp path (not used as default internet route)

## What Problem It Solves
When both NICs have (or had) default routes, Windows can delay traffic while trying the wrong path first.  
This script removes the corp NIC default route and sets interface metrics so internet-bound traffic stays on the internet NIC.

## Prerequisites
- Run in **Administrator PowerShell**
- Interface names must match:
  - `Ethernet` (corp/private NIC)
  - `Ethernet 2` (internet NIC)

## Script Flow
1. ` $ErrorActionPreference = "Stop"`  
Stops on non-suppressed errors so failures are visible.

2. Remove default route from corp NIC (`Ethernet`) in active routes:
```powershell
Get-NetRoute ... -InterfaceAlias "Ethernet" | Remove-NetRoute
```

3. Remove persistent default route from corp NIC:
```powershell
Get-NetRoute -PolicyStore PersistentStore ... | Remove-NetRoute
```

4. Disable automatic metrics and pin manual metrics:
```powershell
Set-NetIPInterface "Ethernet 2" ... -InterfaceMetric 10
Set-NetIPInterface "Ethernet"   ... -InterfaceMetric 500
```
- Lower metric wins, so `Ethernet 2` is preferred.

5. Print verification tables:
- Current default routes (`0.0.0.0/0`)
- Current interface metrics

6. Run quick path check:
```powershell
tracert -d -h 4 8.8.8.8
```
Confirms first hop is your internet gateway (`192.168.16.1` in your environment).

## Expected Good Output
- Only one default route remains: `Ethernet 2 -> 192.168.16.1`
- `Ethernet 2` metric = `10`
- `Ethernet` metric = `500`
- Traceroute first hop = `192.168.16.1`

## Rollback (if needed)
Run as admin and restore auto metrics:
```powershell
Set-NetIPInterface -InterfaceAlias "Ethernet 2" -AddressFamily IPv4 -AutomaticMetric Enabled
Set-NetIPInterface -InterfaceAlias "Ethernet"   -AddressFamily IPv4 -AutomaticMetric Enabled
```

If you intentionally need a corp-side default route again:
```powershell
New-NetRoute -DestinationPrefix "0.0.0.0/0" -InterfaceAlias "Ethernet" -NextHop "10.0.0.254" -PolicyStore PersistentStore
```

## Notes
- This script changes **routing/metrics only**. It does not change DNS server settings.
- If app delays continue, next check is DNS behavior (corp DNS forwarding for public domains).
