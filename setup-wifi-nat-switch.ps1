# Script to setup HYD-InterNet as Internal NAT Switch for Wi-Fi sharing
# IMPORTANT: Run from Elevated PowerShell (Admin)

Write-Host "--- Hyper-V Wi-Fi NAT Switch Setup ---" -ForegroundColor Cyan

# 1. Ensure HYD-InterNet switch exists as Internal
$switchName = "HYD-InterNet"
$existingSwitch = Get-VMSwitch -Name $switchName -ErrorAction SilentlyContinue

if ($existingSwitch) {
    if ($existingSwitch.SwitchType -ne 'Internal') {
        Write-Host "Re-creating $switchName as Internal switch..." -ForegroundColor Yellow
        Remove-VMSwitch -Name $switchName -Force -ErrorAction SilentlyContinue
        New-VMSwitch -Name $switchName -SwitchType Internal | Out-Null
    }
} else {
    Write-Host "Creating Internal switch $switchName..." -ForegroundColor Yellow
    New-VMSwitch -Name $switchName -SwitchType Internal | Out-Null
}

# 2. Get vEthernet interface for HYD-InterNet
$netAdapter = Get-NetAdapter -Name "*$switchName*" -ErrorAction SilentlyContinue

if (-not $netAdapter) {
    Write-Error "Could not find virtual adapter for $switchName"
    return
}

# 3. Assign IP Gateway Address to host's vEthernet (HYD-InterNet) interface (e.g. 192.168.16.1/24)
$gatewayIp = "192.168.16.1"
$prefixLength = 24

$existingIp = Get-NetIPAddress -InterfaceAlias $netAdapter.Name -AddressFamily IPv4 -ErrorAction SilentlyContinue
if ($existingIp) {
    Remove-NetIPAddress -InterfaceAlias $netAdapter.Name -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
}

Write-Host "Assigning Gateway IP $gatewayIp/$prefixLength to $($netAdapter.Name)..." -ForegroundColor Yellow
New-NetIPAddress -InterfaceAlias $netAdapter.Name -IPAddress $gatewayIp -PrefixLength $prefixLength -Confirm:$false | Out-Null

# 4. Create Windows NAT Object for 192.168.16.0/24 subnet
$natName = "HYD-Lab-NAT"
$existingNat = Get-NetNat -Name $natName -ErrorAction SilentlyContinue

if ($existingNat) {
    Remove-NetNat -Name $natName -Confirm:$false -ErrorAction SilentlyContinue
}

Write-Host "Creating Windows NAT for subnet 192.168.16.0/24..." -ForegroundColor Green
New-NetNat -Name $natName -InternalIPInterfaceAddressPrefix "192.168.16.0/24" | Out-Null

Write-Host "--- Setup Complete! ---" -ForegroundColor Green
Write-Host "Your VMs connected to '$switchName' can now access your Wi-Fi via Gateway $gatewayIp" -ForegroundColor Green
