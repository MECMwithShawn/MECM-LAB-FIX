# One-time KVM finishing steps for a lab server; runs as SYSTEM from a scheduled task (see lab-setup.ps1).
# Installs the virtio/SPICE guest tools, re-applies static networking, resets the evaluation
# clock and reboots. Log: C:\lab-setup.log
Start-Transcript -Path C:\lab-setup.log -Append
$cd = (Get-Volume -FileSystemLabel LABFIX).DriveLetter + ':'

Write-Host '== guest tools'
$p = Start-Process "$cd\virtio-win-guest-tools.exe" '/quiet /norestart' -Wait -PassThru
Write-Host "installer exit code $($p.ExitCode)"

Write-Host '== network'
& "$cd\fix-network.ps1"

Write-Host '== evaluation licence'
cscript //nologo "$env:windir\System32\slmgr.vbs" /dlv | Select-String 'rearm|expir|License Status'
cscript //nologo "$env:windir\System32\slmgr.vbs" /rearm

Unregister-ScheduledTask -TaskName LabFinish -Confirm:$false -ErrorAction SilentlyContinue
Stop-Transcript
shutdown /r /t 10 /c 'Lab setup finished; rebooting to apply the licence rearm'
