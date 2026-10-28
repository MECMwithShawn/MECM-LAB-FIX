# Creates and links the "Lab - VM guest integration" GPO at the domain root (run on DC1, elevated).
# - Power button = Shut down and shutdown allowed from the sign-in/lock screen, so a safe shutdown
#   from Lab Manager (ACPI power button) works even on VMs without the guest agent.
# - Startup script: installs the virtio-win guest tools (QEMU guest agent, SPICE agent, drivers)
#   from the lab CD (volume LABFIX) on any VM that doesn't have the QEMU-GA service yet.
#   Takes effect on a VM's next start or two (computer policy first, then the startup script).
$ErrorActionPreference = 'Stop'
$name = 'Lab - VM guest integration'
$domain = Get-ADDomain
$gpo = Get-GPO -Name $name -ErrorAction SilentlyContinue
if (-not $gpo) {
    $gpo = New-GPO -Name $name -Comment 'KVM lab: power button shuts down (also from the lock screen); startup script installs the virtio-win guest tools from the lab CD.'
}

# Startup script in SYSVOL (PowerShell scripts are listed in psscripts.ini)
$root = "\\$($domain.DNSRoot)\SYSVOL\$($domain.DNSRoot)\Policies\{$($gpo.Id)}\Machine\Scripts"
New-Item -ItemType Directory -Force "$root\Startup" | Out-Null
Set-Content -Path "$root\Startup\install-guest-tools.ps1" -Encoding UTF8 -Value @'
# Lab startup script: install the virtio-win guest tools once, from the lab CD.
if (Get-Service QEMU-GA -ErrorAction SilentlyContinue) { return }
$cd = (Get-Volume -FileSystemLabel LABFIX -ErrorAction SilentlyContinue | Select-Object -First 1).DriveLetter
if (-not $cd) { return }
$log = "$env:SystemRoot\Temp\lab-guest-tools.log"
"$(Get-Date -Format s) installing guest tools from ${cd}:" | Out-File $log -Append
$p = Start-Process "${cd}:\virtio-win-guest-tools.exe" '/quiet /norestart' -Wait -PassThru
"$(Get-Date -Format s) exit code $($p.ExitCode)" | Out-File $log -Append
'@
$ini = "[Startup]`r`n0CmdLine=install-guest-tools.ps1`r`n0Parameters=`r`n"
[IO.File]::WriteAllText("$root\psscripts.ini", $ini, [Text.Encoding]::Unicode)
(Get-Item "$root\psscripts.ini" -Force).Attributes = 'Hidden'

# Registry policy settings (these also bump the GPO version so machines pick it up)
$pb = 'HKLM\SOFTWARE\Policies\Microsoft\Power\PowerSettings\7648EFA3-DD9C-4E3E-B566-50F929386280'
Set-GPRegistryValue -Name $name -Key $pb -ValueName ACSettingIndex -Type DWord -Value 3 | Out-Null  # 3 = Shut down
Set-GPRegistryValue -Name $name -Key $pb -ValueName DCSettingIndex -Type DWord -Value 3 | Out-Null
Set-GPRegistryValue -Name $name -Key 'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' `
    -ValueName ShutdownWithoutLogon -Type DWord -Value 1 | Out-Null

# Tell clients the GPO has Scripts (startup) as well as Registry settings: client-side extension
# GUID pairs, each list sorted by GUID.
$ext = '[{35378EAC-683F-11D2-A89A-00C04FBBCFA2}{D02B1F72-3407-48AE-BA88-E8213C6761F1}]' +
       '[{42B5FAAE-6536-11D2-AE5A-0000F87571E3}{40B6664F-4972-11D1-A7CA-0000F87571E3}]'
Set-ADObject -Identity $gpo.Path -Replace @{ gPCMachineExtensionNames = $ext }
# One more version bump after the extension change
Set-GPRegistryValue -Name $name -Key $pb -ValueName ACSettingIndex -Type DWord -Value 3 | Out-Null

if (-not ((Get-GPInheritance -Target $domain.DistinguishedName).GpoLinks | Where-Object DisplayName -eq $name)) {
    New-GPLink -Name $name -Target $domain.DistinguishedName -LinkEnabled Yes | Out-Null
}
$g = Get-GPO -Name $name
"'$name' linked at $($domain.DistinguishedName) · computer version $($g.Computer.DSVersion)/$($g.Computer.SysvolVersion)"
# Undo: Remove-GPLink -Name 'Lab - VM guest integration' -Target (Get-ADDomain).DistinguishedName
