# MECM Lab / Hydration Kit Fix Script
# Resolves Hyper-V error 0x800705B4 (timeout), 0x80070020 (sharing violation), and 0xC03A000E (VHD chain mismatch)
# IMPORTANT: Run this script from an Elevated PowerShell (Admin) session.

Write-Host "--- MECM Lab Fix Started ---" -ForegroundColor Cyan

$labPath = "C:\Win11_25H2_Lab"
if (-not (Test-Path $labPath)) {
    Write-Error "Lab folder '$labPath' was not found. Please ensure the lab is extracted to $labPath."
    return
}

# 1. Kill any orphaned vmwp.exe processes holding file locks
Write-Host "Checking for orphaned Hyper-V worker processes..." -ForegroundColor Yellow
$activeVmGuids = (Get-VM | Select-Object -ExpandProperty Id).Guid
$vmwpProcesses = Get-CimInstance Win32_Process -Filter "Name = 'vmwp.exe'"

foreach ($proc in $vmwpProcesses) {
    if ($proc.CommandLine -match "([a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})") {
        $vmGuid = $Matches[1]
        if ($activeVmGuids -notcontains $vmGuid) {
            Write-Host "Terminating orphaned worker process (PID: $($proc.ProcessId))..." -ForegroundColor Yellow
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

# 2. Fix HYD-GW1 if chain mismatch or corrupted checkpoint
$gw1Vhdx = "$labPath\HYD-GW1\Virtual Hard Disks\HYD-GW1.VHDX"
$serverParent = "$labPath\ServerParent.vhdx"

if (Test-Path $gw1Vhdx) {
    Set-VHD -Path $gw1Vhdx -ParentPath $serverParent -IgnoreMismatch -ErrorAction SilentlyContinue
    
    $gw1Vm = Get-VM -Name "HYD-GW1" -ErrorAction SilentlyContinue
    if ($gw1Vm -and $gw1Vm.State -ne 'Running') {
        # Check if GW1 has startup error
        try {
            Start-VM -Name "HYD-GW1" -ErrorAction Stop
        } catch {
            Write-Host "Re-creating clean HYD-GW1 VM definition..." -ForegroundColor Yellow
            Remove-VM -Name "HYD-GW1" -Force
            New-VM -Name "HYD-GW1" -MemoryStartupBytes 2GB -Generation 2 -VHDPath $gw1Vhdx -SwitchName "HYD-CorpNet" | Out-Null
            Add-VMNetworkAdapter -VMName "HYD-GW1" -Name "External1" -SwitchName "HYD-InterNet" | Out-Null
            Set-VMMemory -VMName "HYD-GW1" -DynamicMemoryEnabled $true -MinimumBytes 1GB -MaximumBytes 4GB
            Set-VMProcessor -VMName "HYD-GW1" -Count 2
            Start-VM -Name "HYD-GW1" -ErrorAction SilentlyContinue
        }
    }
}

# 3. Re-create clean VM definitions for each client VM
Write-Host "Re-creating clean client VM definitions..." -ForegroundColor Yellow
$clientNames = @("HYD-CLIENT1", "HYD-CLIENT2", "HYD-CLIENT3", "HYD-CLIENT4", "HYD-Client5", "HYD-Client6")

foreach ($cName in $clientNames) {
    $vhdFile = Get-ChildItem -Path $labPath -Filter "$cName*.vhdx" | Select-Object -First 1
    if (-not $vhdFile) { continue }

    $existingVms = Get-VM -Name $cName -ErrorAction SilentlyContinue
    foreach ($vm in $existingVms) {
        if ($vm.State -eq 'Running') { Stop-VM -Name $vm.Name -TurnOff -Force -ErrorAction SilentlyContinue }
        Remove-VM -Name $vm.Name -Force -ErrorAction SilentlyContinue
    }

    New-VM -Name $cName -MemoryStartupBytes 2GB -Generation 2 -VHDPath $vhdFile.FullName -SwitchName "HYD-CorpNet" | Out-Null
    Enable-VMTpm -VMName $cName
    Set-VMMemory -VMName $cName -DynamicMemoryEnabled $true -MinimumBytes 2GB -MaximumBytes 4GB
    Set-VMProcessor -VMName $cName -Count 2
}

# 4. Boot Core Lab VMs
Write-Host "Starting core lab VMs..." -ForegroundColor Cyan
Start-VM -Name "HYD-GW1" -ErrorAction SilentlyContinue
Start-VM -Name "HYD-DC1" -ErrorAction SilentlyContinue
Start-VM -Name "HYD-CM1" -ErrorAction SilentlyContinue
Start-VM -Name "HYD-CLIENT1" -ErrorAction SilentlyContinue

Write-Host "--- Current Hyper-V VM Status ---" -ForegroundColor Cyan
Get-VM | Select-Object Name, State, CpuUsage, MemoryAssigned | Format-Table -AutoSize
Write-Host "--- Fix Complete ---" -ForegroundColor Green
