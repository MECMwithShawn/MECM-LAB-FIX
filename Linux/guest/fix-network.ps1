# Re-applies the lab's static network settings after the move from Hyper-V to KVM.
# The VMs now have Intel e1000e adapters instead of Hyper-V ones, so Windows sees new
# adapters with DHCP. Run once on DC1, GW1 and CM1 in an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File D:\fix-network.ps1
# Clients use DHCP from DC1 and need nothing.
$ErrorActionPreference = 'Stop'

# Internet-side DNS is libvirt's resolver on the NAT network (192.168.16.1): it follows whatever
# network the laptop is on, and the work Wi-Fi blocks public resolvers like 8.8.8.8.
# CM1 metrics (internet 10, lab 500) are from MECM-LAB-FIX fix-cm1-routing.ps1.
# MAC prefixes come from make-vms.py: 52:54:00:10:00:xx = HYD-CorpNet, 52:54:00:16:00:xx = HYD-InterNet
$config = @{
    'DC1' = @(
        @{ Mac = '52-54-00-10-00'; Name = 'Ethernet'; IP = '10.0.0.6'; Gateway = '10.0.0.254'; Dns = @('127.0.0.1') }
    )
    'GW1' = @(
        @{ Mac = '52-54-00-10-00'; Name = 'Ethernet'; IP = '10.0.0.254'; Dns = @('10.0.0.6') }
        @{ Mac = '52-54-00-16-00'; Name = 'Ethernet 2'; IP = '192.168.16.254'; Gateway = '192.168.16.1'; Dns = @('192.168.16.1') }
    )
    'CM1' = @(
        @{ Mac = '52-54-00-10-00'; Name = 'Ethernet'; IP = '10.0.0.7'; Dns = @('10.0.0.6'); Metric = 500 }
        @{ Mac = '52-54-00-16-00'; Name = 'Ethernet 2'; IP = '192.168.16.7'; Gateway = '192.168.16.1'; Dns = @('192.168.16.1'); Metric = 10 }
    )
}

$mine = $config[$env:COMPUTERNAME]
if (-not $mine) { Write-Host "$env:COMPUTERNAME uses DHCP; nothing to do."; return }

# Drop the Hyper-V adapters that no longer exist so their names and addresses are free again.
Get-PnpDevice -Class Net | Where-Object { $_.Status -eq 'Unknown' -and $_.FriendlyName -like '*Hyper-V*' } | ForEach-Object {
    pnputil /remove-device $_.InstanceId | Out-Null
    Write-Host "Removed old adapter: $($_.FriendlyName)"
}

foreach ($n in $mine) {
    $adapter = Get-NetAdapter | Where-Object { $_.MacAddress.StartsWith($n.Mac) }
    if (-not $adapter) { throw "No adapter with MAC $($n.Mac)-xx found" }
    if ($adapter.Name -ne $n.Name) { Rename-NetAdapter -Name $adapter.Name -NewName $n.Name }
    Set-NetIPInterface -InterfaceAlias $n.Name -AddressFamily IPv4 -Dhcp Disabled
    Get-NetIPAddress -InterfaceAlias $n.Name -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false
    Get-NetRoute -InterfaceAlias $n.Name -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false
    $ip = @{ InterfaceAlias = $n.Name; IPAddress = $n.IP; PrefixLength = 24 }
    if ($n.Gateway) { $ip.DefaultGateway = $n.Gateway }
    New-NetIPAddress @ip | Out-Null
    Set-DnsClientServerAddress -InterfaceAlias $n.Name -ServerAddresses $n.Dns
    if ($n.Metric) { Set-NetIPInterface -InterfaceAlias $n.Name -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric $n.Metric }
    Write-Host "$($n.Name): $($n.IP)"
}

if ($env:COMPUTERNAME -eq 'DC1') { Set-DnsServerForwarder -IPAddress 192.168.16.1 -UseRootHint $true }

if ($env:COMPUTERNAME -eq 'GW1') {
    # RRAS NAT was bound to the old Hyper-V adapters by ID; bind it to the new ones.
    netsh routing ip nat add interface name="Ethernet 2" mode=full
    netsh routing ip nat add interface name="Ethernet" mode=private
    Restart-Service RemoteAccess
    Write-Host 'NAT rebound: Ethernet 2 = internet, Ethernet = lab. Test with: Test-NetConnection 1.1.1.1 -Port 443'
}
Write-Host 'Done.'
