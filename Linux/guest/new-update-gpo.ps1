# Creates and links the "Lab - Pause Windows Update" GPO at the domain root (run on DC1, elevated).
# Every lab VM is a domain member, so this reaches all of them.
#
# - No automatic updates, no Windows Update / Store downloads from the internet: saves the
#   laptop's Wi-Fi and keeps the lab baseline.
# - wuauserv is NOT disabled and no WSUS server is set here: once CM1's Software Update Point is
#   configured, the ConfigMgr client points machines at CM1's WSUS itself, and a WSUS address in a
#   domain GPO would conflict with it. So updates can still come from CM1.
# - Unlink it for lab 5.1 (Servicing Windows 11 using Group Policy).
$name = 'Lab - Pause Windows Update'
$domain = (Get-ADDomain).DistinguishedName
$gpo = Get-GPO -Name $name -ErrorAction SilentlyContinue
if (-not $gpo) {
    $gpo = New-GPO -Name $name -Comment 'Lab VMs: no automatic or internet Windows Update. wuauserv stays on so ConfigMgr/WSUS on CM1 still works. Unlink for lab 5.1.'
}
$wu = 'HKLM\Software\Policies\Microsoft\Windows\WindowsUpdate'
Set-GPRegistryValue -Name $name -Key "$wu\AU" -ValueName NoAutoUpdate -Type DWord -Value 1 | Out-Null
Set-GPRegistryValue -Name $name -Key $wu -ValueName DoNotConnectToWindowsUpdateInternetLocations -Type DWord -Value 1 | Out-Null
Set-GPRegistryValue -Name $name -Key $wu -ValueName SetDisableUXWUAccess -Type DWord -Value 1 | Out-Null
Set-GPRegistryValue -Name $name -Key $wu -ValueName ExcludeWUDriversInQualityUpdate -Type DWord -Value 1 | Out-Null
Set-GPRegistryValue -Name $name -Key 'HKLM\Software\Policies\Microsoft\WindowsStore' -ValueName AutoDownload -Type DWord -Value 2 | Out-Null
if (-not ((Get-GPInheritance -Target $domain).GpoLinks | Where-Object DisplayName -eq $name)) {
    New-GPLink -Name $name -Target $domain -LinkEnabled Yes | Out-Null
}
Write-Host "'$name' linked at $domain. Clients apply it at their next policy refresh or reboot; run 'gpupdate /force' to apply now."
# Undo:  Remove-GPLink -Name 'Lab - Pause Windows Update' -Target (Get-ADDomain).DistinguishedName
