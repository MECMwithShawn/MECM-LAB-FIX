# MECM Lab Guest VM - Disable Automatic Windows Updates Script
# Disables Windows Update service, Update Orchestrator, and registry policies inside guest VMs.
# IMPORTANT: Run this script inside a Guest VM (or remotely via Invoke-Command) as Administrator.

Write-Host "--- Disabling Automatic Windows Updates ---" -ForegroundColor Cyan

# 1. Set Windows Update Group Policy Registry Keys
$auPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
if (-not (Test-Path $auPath)) {
    New-Item -Path $auPath -Force | Out-Null
}

Write-Host "Setting Group Policy registry keys to disable auto-updates..." -ForegroundColor Yellow
Set-ItemProperty -Path $auPath -Name "NoAutoUpdate" -Value 1 -Type DWord -Force
Set-ItemProperty -Path $auPath -Name "AUOptions" -Value 1 -Type DWord -Force

# Do not connect to Windows Update internet locations
$wuPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
Set-ItemProperty -Path $wuPath -Name "DoNotConnectToWindowsUpdateInternetLocations" -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue

# 2. Stop and Disable Services
Write-Host "Stopping and disabling Windows Update services..." -ForegroundColor Yellow
$services = @("wuauserv", "usoServ", "bits", "dosvc")

foreach ($svc in $services) {
    if (Get-Service -Name $svc -ErrorAction SilentlyContinue) {
        Stop-Service -Name $svc -Force -ErrorAction SilentlyContinue
        Set-Service -Name $svc -StartupType Disabled -ErrorAction SilentlyContinue
        Write-Host "Disabled service: $svc" -ForegroundColor Green
    }
}

# 3. Disable Scheduled Tasks
Write-Host "Disabling Windows Update scheduled tasks..." -ForegroundColor Yellow
Get-ScheduledTask -TaskPath '\Microsoft\Windows\UpdateOrchestrator\*' -ErrorAction SilentlyContinue | Disable-ScheduledTask -ErrorAction SilentlyContinue
Get-ScheduledTask -TaskPath '\Microsoft\Windows\WindowsUpdate\*' -ErrorAction SilentlyContinue | Disable-ScheduledTask -ErrorAction SilentlyContinue

Write-Host "--- Windows Update Successfully Disabled ---" -ForegroundColor Green
