# MECM Lab / Hydration Kit Cleanup Script
# This script removes VMs and Virtual Switches created by the Win11 25H2 Lab setup.
# IMPORTANT: Run this script from an Elevated PowerShell (Admin) session.

Write-Host "--- MECM Lab Cleanup Started ---" -ForegroundColor Cyan

# 1. Stop and Remove VMs starting with HYD-
$vms = Get-VM | Where-Object { $_.Name -like "HYD-*" }

if ($vms) {
    Write-Host "Found $($vms.Count) Lab VMs. Stopping and removing them..." -ForegroundColor Yellow
    foreach ($vm in $vms) {
        try {
            if ($vm.State -eq 'Running') {
                Write-Host "Stopping VM: $($vm.Name)..."
                Stop-VM -Name $vm.Name -Force -Confirm:$false
            }
            Write-Host "Removing VM: $($vm.Name)..."
            Remove-VM -Name $vm.Name -Force -Confirm:$false
        } catch {
            Write-Error "Failed to remove VM $($vm.Name): $_"
        }
    }
} else {
    Write-Host "No VMs starting with 'HYD-' found." -ForegroundColor Gray
}

# 2. Remove Lab Virtual Switches
$switches = Get-VMSwitch | Where-Object { $_.Name -match "HYD-CorpNet" -or $_.Name -match "HYD-InterNet" }

if ($switches) {
    Write-Host "Found $($switches.Count) Lab Switches. Removing them..." -ForegroundColor Yellow
    foreach ($sw in $switches) {
        try {
            Write-Host "Removing Switch: $($sw.Name)..."
            Remove-VMSwitch -Name $sw.Name -Force -Confirm:$false
        } catch {
            Write-Error "Failed to remove switch $($sw.Name): $_. This may happen if a VM is still using it."
        }
    }
} else {
    Write-Host "No Lab Switches found." -ForegroundColor Gray
}

Write-Host "--- Cleanup Complete ---" -ForegroundColor Green
Write-Host "You can now run 'C:\MECM Lab\setup.exe' to reinstall the lab."
