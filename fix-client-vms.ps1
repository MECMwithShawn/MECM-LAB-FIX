# MECM Lab / Hydration Kit Client VM Fix Script
# Resolves Hyper-V error 0x800705B4 (timeout) and 0x80070020 (sharing violation) on HYD-CLIENT VMs
# IMPORTANT: Run this script from an Elevated PowerShell (Admin) session.

Write-Host "--- MECM Lab Client VM Fix Started ---" -ForegroundColor Cyan

$labPath = "C:\Win11_25H2_Lab"
$clientNames = @("HYD-CLIENT1", "HYD-CLIENT2", "HYD-CLIENT3", "HYD-CLIENT4", "HYD-Client5", "HYD-Client6")

if (-not (Test-Path $labPath)) {
    Write-Error "Lab folder '$labPath' was not found. Please ensure the lab is extracted to $labPath."
    return
}

# 1. Kill any orphaned vmwp.exe processes holding file locks on client VHDX files
Write-Host "Checking for orphaned Hyper-V worker processes..." -ForegroundColor Yellow
$activeVmGuids = (Get-VM | Select-Object -ExpandProperty Id).Guid
$vmwpProcesses = Get-CimInstance Win32_Process -Filter "Name = 'vmwp.exe'"

foreach ($proc in $vmwpProcesses) {
    # Extract GUID from command line
    if ($proc.CommandLine -match "([a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})") {
        $vmGuid = $Matches[1]
        if ($activeVmGuids -notcontains $vmGuid) {
            Write-Host "Terminating orphaned worker process (PID: $($proc.ProcessId), VMID: $vmGuid)..." -ForegroundColor Yellow
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

# 2. Re-create clean VM definitions for each client VM
Write-Host "Re-creating clean client VM definitions..." -ForegroundColor Yellow

foreach ($cName in $clientNames) {
    # Locate VHDX file in lab directory
    $vhdFile = Get-ChildItem -Path $labPath -Filter "$cName*.vhdx" | Select-Object -First 1
    if (-not $vhdFile) {
        Write-Host "VHDX for $cName not found in $labPath. Skipping..." -ForegroundColor Gray
        continue
    }

    # Force stop and remove existing VM definition if present
    $existingVms = Get-VM -Name $cName -ErrorAction SilentlyContinue
    foreach ($vm in $existingVms) {
        if ($vm.State -eq 'Running') {
            Write-Host "Stopping existing $cName..."
            Stop-VM -Name $vm.Name -TurnOff -Force -ErrorAction SilentlyContinue
        }
        Write-Host "Removing old VM definition for $cName..."
        Remove-VM -Name $vm.Name -Force -ErrorAction SilentlyContinue
    }

    # Recreate clean Generation 2 VM
    Write-Host "Creating clean Gen2 VM: $cName..." -ForegroundColor Green
    New-VM -Name $cName -MemoryStartupBytes 2GB -Generation 2 -VHDPath $vhdFile.FullName -SwitchName "HYD-CorpNet" | Out-Null
    
    # Configure vTPM, Dynamic Memory, and 2 Processors
    Enable-VMTpm -VMName $cName
    Set-VMMemory -VMName $cName -DynamicMemoryEnabled $true -MinimumBytes 2GB -MaximumBytes 4GB
    Set-VMProcessor -VMName $cName -Count 2
}

# 3. Test start HYD-CLIENT1
Write-Host "Attempting to start HYD-CLIENT1..." -ForegroundColor Cyan
try {
    Start-VM -Name "HYD-CLIENT1" -ErrorAction Stop
    Write-Host "HYD-CLIENT1 started successfully!" -ForegroundColor Green
} catch {
    Write-Error "Failed to start HYD-CLIENT1: $_"
}

Write-Host "--- Current Hyper-V VM Status ---" -ForegroundColor Cyan
Get-VM | Select-Object Name, State, CpuUsage, MemoryAssigned | Format-Table -AutoSize
Write-Host "--- Fix Complete ---" -ForegroundColor Green
