# MECM Lab Fix

Fixes, scripts and notes for running the **Windows 11 and Office 365 Deployment Lab Kit**
(25H2: DC1, GW1, CM1, CLIENT1–6) on a Wi-Fi laptop.

**Lab Link:** [Windows 11 and Office 365 Deployment Lab Kit](https://learn.microsoft.com/en-us/microsoft-365/enterprise/modern-desktop-deployment-and-management-lab?view=o365-worldwide)

| Host | Folder | What's there |
| :--- | :--- | :--- |
| **Windows (Hyper-V)** | [`Windows/`](./Windows) | The original fixes: client VM start errors (`0x800705B4`), Wi-Fi-safe NAT switch, CM1 dual-NIC routing, blocked public DNS, guest updates, cleanup, guest Wi-Fi workarounds. Start with [`Windows/README.md`](./Windows/README.md) and [`Windows/LAB_REFERENCE_GUIDE.md`](./Windows/LAB_REFERENCE_GUIDE.md). |
| **Linux (KVM / libvirt)** | [`Linux/`](./Linux) | Moving the provisioned Hyper-V lab onto Linux: disk-chain conversion, libvirt networks and VMs, a one-shot first-boot script, Hyper-V-style checkpoints, and running commands in VMs with no sign-in. Start with [`Linux/README.md`](./Linux/README.md). |

## Same network design on both

Both keep the lab off the work Wi-Fi in the same way: **the VMs reach the internet only through
NAT on the host** on `192.168.16.0/24`, so the Wi-Fi only ever sees the laptop's own MAC. The lab
network `10.0.0.0/24` (HYD-CorpNet) has no link to the host network.

| VM | Lab IP | Internet side |
| :--- | :--- | :--- |
| HYD-DC1 (AD, DNS, DHCP) | `10.0.0.6` | via GW1 |
| HYD-GW1 (RRAS NAT) | `10.0.0.254` | `192.168.16.254` → host `192.168.16.1` |
| HYD-CM1 (ConfigMgr) | `10.0.0.7` | `192.168.16.7` (metric 10) |
| HYD-CLIENT1–6 | DHCP `10.0.0.100–200` | via GW1 |

The work Wi-Fi blocks public DNS (8.8.8.8 / 1.1.1.1). On Hyper-V that's handled by pointing DNS at
the Wi-Fi's own resolvers ([Windows Issue 7](./Windows/LAB_REFERENCE_GUIDE.md)). On Linux, DNS goes
to the host's NAT resolver `192.168.16.1`, which follows whatever network the laptop is on.

## Windows Update in the lab

[`Linux/guest/new-update-gpo.ps1`](./Linux/guest/new-update-gpo.ps1) creates the domain GPO
**Lab - Pause Windows Update** (works on either host; run it on DC1). It stops automatic and
internet updates but keeps the Windows Update service running, so updates can still come from
CM1's WSUS / Software Update Point. It's a gentler alternative to
[`Windows/disable-guest-updates.ps1`](./Windows/disable-guest-updates.ps1), which disables the
service and so also blocks ConfigMgr update scans.

## Credentials

The kit's published defaults are in the official set-up guide and in
[`Windows/LAB_REFERENCE_GUIDE.md`](./Windows/LAB_REFERENCE_GUIDE.md).
