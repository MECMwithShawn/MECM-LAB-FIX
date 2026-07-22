# MECM Deployment Lab Kit — Master Reference & Troubleshooting Guide

This reference document provides a complete guide to architecture, network topology, common Hyper-V errors, and fix scripts for the **Windows 11 and M365 Deployment Lab Kit**.

---

## 1. Lab Architecture & Network Topology

The lab operates on two distinct network boundaries:

```mermaid
graph TD
    subgraph Physical Host
        HostWiFi["Host Wi-Fi Card (Work Wi-Fi / Internet)"]
        vEthNAT["vEthernet (HYD-InterNet) <br> IP: 192.168.16.1 / 24 <br> Windows NAT: HYD-Lab-NAT"]
        HostWiFi --- vEthNAT
    end

    subgraph Hyper-V Virtual Switches
        NatSwitch["HYD-InterNet Switch (Internal)"]
        CorpSwitch["HYD-CorpNet Switch (Private)"]
        vEthNAT --- NatSwitch
    end

    subgraph Router VM
        GW1["HYD-GW1 (Router / Gateway VM) <br> External1: 192.168.16.254 <br> HYD-Corpnet0: 10.0.0.254"]
    end

    subgraph Corporate Lab Network (10.0.0.0/24)
        DC1["HYD-DC1 (DC / DNS) <br> IP: 10.0.0.6 <br> GW: 10.0.0.254"]
        CM1["HYD-CM1 (ConfigMgr Primary Site) <br> IP: 10.0.0.7 <br> GW: 10.0.0.254"]
        CLIENT1["HYD-CLIENT1 (Win 11 Client) <br> IP: 10.0.0.107 <br> GW: 10.0.0.254"]
        CLIENTS["HYD-CLIENT2 to CLIENT6"]
    end

    NatSwitch <-->|External1 NIC| GW1
    GW1 <-->|HYD-Corpnet0 NIC| CorpSwitch
    CorpSwitch <--> DC1
    CorpSwitch <--> CM1
    CorpSwitch <--> CLIENT1
    CorpSwitch <--> CLIENTS
```

### VM IP Mapping & Roles Table

| VM Name | Role | Corporate IP | Gateway IP | Virtual Switch Bindings |
| :--- | :--- | :--- | :--- | :--- |
| **`HYD-GW1`** | RRAS Router / NAT Gateway | `10.0.0.254` | `192.168.16.1` | `HYD-CorpNet` & `HYD-InterNet` (Internal) |
| **`HYD-DC1`** | Active Directory & DNS | `10.0.0.6` | `10.0.0.254` | `HYD-CorpNet` |
| **`HYD-CM1`** | ConfigMgr Primary Site Server | `10.0.0.7` | `10.0.0.254` | `HYD-CorpNet` |
| **`HYD-CLIENT1..6`**| Windows 11 Workstations | `10.0.0.100+` | `10.0.0.254` | `HYD-CorpNet` |

---

## 2. Master Troubleshooting & Fix Registry

### Issue 1: Client VM Startup Error (`0x800705B4` Timeout / `0x80070020` File Lock)
* **Symptom**: Starting client VMs throws `'HYD-CLIENT1' failed to start worker process: This operation returned because the timeout period expired. (0x800705B4)`.
* **Root Cause**:
  1. Orphaned `vmwp.exe` worker processes remaining from setup maintain open file locks (`0x80070020`) on `WindowsParent.vhdx`.
  2. Corrupted `.vmgs` guest state files in `C:\ProgramData\Microsoft\Windows\Hyper-V` cause worker initialization to hang.
* **Fix Script**: [`fix-client-vms.ps1`](./fix-client-vms.ps1)
* **Action**: Terminates orphaned `vmwp.exe` processes, clears corrupted VM metadata, recreates clean Generation 2 VM definitions with vTPM enabled, and starts `HYD-CLIENT1`.

```powershell
powershell -ExecutionPolicy Bypass -File .\fix-client-vms.ps1
```

---

### Issue 2: Work Wi-Fi Disconnects when Creating External Switch
* **Symptom**: Selecting a Wi-Fi card in Hyper-V Virtual Switch Manager drops host Wi-Fi connection instantly.
* **Root Cause**: Wi-Fi 802.11 protocol restriction permits only **1 MAC address per wireless association**. External switches try to pass multiple VM MACs through 1 Wi-Fi card, causing enterprise access points to sever the connection.
* **Fix Script**: [`setup-wifi-nat-switch.ps1`](./setup-wifi-nat-switch.ps1)
* **Action**: Converts `HYD-InterNet` to an **Internal Switch**, configures Host vEthernet to `192.168.16.1/24`, and enables Windows NAT (`HYD-Lab-NAT` on `192.168.16.0/24`). All VMs share Wi-Fi seamlessly using your host's single authenticated MAC address.

```powershell
powershell -ExecutionPolicy Bypass -File .\setup-wifi-nat-switch.ps1
```

---

### Issue 3: VHD Differencing Chain Mismatch (`0xC03A000E`)
* **Symptom**: VM boot fails with `The chain of virtual hard disks is corrupted. There is a mismatch in the identifiers of the parent virtual hard disk and differencing disk. (0xC03A000E)`.
* **Root Cause**: Differencing disk (`.vhdx` / `.avhdx`) parent ID changed when an underlying parent disk was modified or snapshot chain was interrupted.
* **Fix Action**: Re-link parent VHD chain using PowerShell `-IgnoreMismatch`:

```powershell
# Run from Elevated PowerShell
Set-VHD -Path "C:\Win11_25H2_Lab\HYD-GW1\Virtual Hard Disks\HYD-GW1.VHDX" -ParentPath "C:\Win11_25H2_Lab\ServerParent.vhdx" -IgnoreMismatch
```

---

### Issue 4: Dual-NIC CM1 Slow Internet Traffic
* **Symptom**: `HYD-CM1` internet traffic is sluggish or times out due to competing network interfaces.
* **Root Cause**: A default route (`0.0.0.0/0`) exists on the private/corp NIC with equal interface metrics to the internet NIC.
* **Fix Script**: [`fix-cm1-routing.ps1`](./fix-cm1-routing.ps1)
* **Action**: Removes default route from private NIC (`Ethernet`) and sets metric `10` on `Ethernet 2` (internet) vs `500` on `Ethernet` (corp).

```powershell
powershell -ExecutionPolicy Bypass -File .\fix-cm1-routing.ps1
```

---

### Issue 5: Automatic Windows Updates Disrupting Lab Baseline
* **Symptom**: Guest VMs consume background bandwidth and auto-reboot due to Windows Update.
* **Root Cause**: Windows 11 guest OS reaching out to Microsoft Update servers via NAT gateway.
* **Fix Script**: [`disable-guest-updates.ps1`](./disable-guest-updates.ps1)
* **Action**: Sets Group Policy registry keys (`NoAutoUpdate = 1`), disables `wuauserv` & `usoServ` services, and disables UpdateOrchestrator scheduled tasks. Run inside guest VM.

```powershell
powershell -ExecutionPolicy Bypass -File .\disable-guest-updates.ps1
```

---

### Issue 6: Complete Lab Reset & Reinstallation
* **Symptom**: Need to wipe existing lab VMs and switches to perform a clean setup.
* **Fix Script**: [`cleanup_lab.ps1`](./cleanup_lab.ps1)
* **Action**: Stops and removes all `HYD-*` VMs and cleans up `HYD-CorpNet` & `HYD-InterNet` switches.

```powershell
powershell -ExecutionPolicy Bypass -File .\cleanup_lab.ps1
```

---

## 3. Quick-Start Execution Order

For a fresh setup on a Wi-Fi connected laptop:

1. **Clean Reinstall (if needed)**: Run `cleanup_lab.ps1` -> Run `C:\MECM Lab\setup.exe`.
2. **Setup Wi-Fi NAT**: Run `setup-wifi-nat-switch.ps1`.
3. **Fix Client VMs**: Run `fix-client-vms.ps1`.
4. **Fix CM1 Routing**: Run `fix-cm1-routing.ps1` on CM1.
5. **Disable Guest Auto-Updates**: Run `disable-guest-updates.ps1` on guest VMs.
