# Run once on DC1 in an elevated PowerShell, signed in as a domain admin:
#   powershell -ExecutionPolicy Bypass -File D:\lab-setup.ps1
# Runs server-finish.ps1 on GW1 and CM1 (through remoting, as a SYSTEM scheduled task so it
# survives their IP change and reboot), then on DC1 itself, which reboots at the end.
$ErrorActionPreference = 'Stop'
$run = {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
        '-ExecutionPolicy Bypass -Command "& ((Get-Volume -FileSystemLabel LABFIX).DriveLetter + '':\server-finish.ps1'')"')
    Register-ScheduledTask -TaskName LabFinish -Action $action -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
    Start-ScheduledTask -TaskName LabFinish
    "$env:COMPUTERNAME started"
}

Write-Host 'DHCP leases on the lab network (GW1/CM1 should be here until their static IPs return):'
Get-DhcpServerv4Scope | Get-DhcpServerv4Lease | Format-Table IPAddress, HostName, AddressState -AutoSize

foreach ($server in 'GW1', 'CM1') {
    try {
        Invoke-Command -ComputerName $server -ScriptBlock $run
    } catch {
        Write-Warning "${server}: $($_.Exception.Message)"
        Write-Warning "Sign in on $server and run:  powershell -ExecutionPolicy Bypass -File D:\server-finish.ps1"
    }
}

Write-Host 'Now DC1 (it reboots when finished)...'
& $run
