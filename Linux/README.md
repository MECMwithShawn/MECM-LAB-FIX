# Running the lab on Linux (KVM / libvirt)

The **Windows 11 and M365 Deployment Lab Kit (25H2)** ships as Hyper-V VMs. This folder moves an
already-provisioned Hyper-V lab onto a Linux host (tested on Zorin OS 18.1 / Ubuntu 24.04 base, QEMU 8.2,
libvirt 10) and keeps it working on a Wi-Fi-only laptop, the same goals as the [Windows](../Windows) fixes.

All 9 VMs run: DC1, GW1, CM1, CLIENT1–4, and the blank CLIENT5/6 for PXE/OSD.

## Layout

| Folder | What's in it |
| :--- | :--- |
| [`convert/`](./convert) | `vhdxflat.py` merges each Hyper-V checkpoint chain into one disk; `convert-all.sh` does all VMs → qcow2; `verify.py` cross-checks against libvhdi |
| [`vm/`](./vm) | `define-lab.sh` creates the networks and VMs; `make-vms.py` holds the VM specs; `checkpoint.sh` gives Hyper-V-style checkpoints |
| [`guest/`](./guest) | Scripts that run **inside** Windows, delivered on a CD (`lab-fixup.iso`) built by `build-iso.sh` |
| [`manager/`](./manager) | **Lab Manager**: a Windows 11-style app to start, stop and connect to the VMs (`install.sh` adds it to the apps menu) |
| [`tools/`](./tools) | `vmexec.py` runs PowerShell in a VM with no sign-in; `vmtype.py` types into a VM; `shot.sh` screenshots; `peek.sh` reads a VM's offline IP config |

Everything works on `$LAB` (default `~/vms/m365-lab`), which holds `src/`, `disks/`, `xml/` and the ISO.

## Network design (same as the Wi-Fi fix on Hyper-V)

```
Work Wi-Fi (only the laptop's own MAC)          laptop
        │                                          │
   host NAT (libvirt)  ── virbr-hydinet 192.168.16.1/24, DNS forwarder
        │                     ├─ GW1 Ethernet 2  192.168.16.254  (RRAS NAT for the lab)
        │                     └─ CM1 Ethernet 2  192.168.16.7    (metric 10)
   no host link       ── virbr-hydcorp  (isolated, no host IP)
                              ├─ DC1  10.0.0.6   AD / DNS / DHCP 10.0.0.100-200, router .254
                              ├─ GW1  10.0.0.254
                              ├─ CM1  10.0.0.7   (metric 500)
                              └─ CLIENT1-6  DHCP
```

- **Nothing is bridged to Wi-Fi.** HYD-InterNet is libvirt NAT, so the Wi-Fi only ever sees the
  laptop, the same idea as `setup-wifi-nat-switch.ps1`. HYD-CorpNet has no host IP, so DC1's DHCP
  can't leak onto the work network.
- **DNS follows whatever network the laptop is on.** DC1's forwarder and the internet NICs of
  GW1/CM1 point at `192.168.16.1`, libvirt's resolver, which uses the host's DNS. This replaces
  hard-coding `172.64.36.x` (Windows Issue 7: the work Wi-Fi blocks 8.8.8.8 / 1.1.1.1).
- The NAT subnet stays `192.168.16.0/24` because GW1 and CM1 already have static IPs there.

## Fresh setup

### 1. Host packages (once)

```bash
sudo apt install -y libvirt-daemon-system virt-manager swtpm swtpm-tools genisoimage ntfs-3g
sudo usermod -aG libvirt,kvm $USER          # then log out and back in
setfacl -m u:libvirt-qemu:x $HOME           # let QEMU reach disks under your home
gsettings set org.virt-manager.virt-manager.console resize-guest 1   # VM display follows the window
```

### 2. Get the Hyper-V disks

Either provision the lab on Windows with the kit's `setup.exe` (dual boot) and mount that
partition read-only (`udisksctl mount -b /dev/<part> -o ro`), or extract the kit's `.zpaq` with
`zpaq x` (`sudo apt install zpaq`).

Put symlinks to **every** `.vhdx`/`.avhdx` in one flat folder, `$LAB/src`. The disks reference
their parents by relative Windows paths, so they must sit side by side:

```bash
mkdir -p ~/vms/m365-lab/src && cd ~/vms/m365-lab/src
ln -s /media/$USER/OS/Win11_25H2_Lab/*.vhdx /media/$USER/OS/Win11_25H2_Lab/*.VHDx /media/$USER/OS/Win11_25H2_Lab/*.avhdx .
for v in DC1 CM1 GW1; do ln -s "/media/$USER/OS/Win11_25H2_Lab/HYD-$v/Virtual Hard Disks/"* .; done
```

### 3. Convert (about 8 minutes from NVMe; about 280 GB of qcow2)

```bash
Linux/convert/convert-all.sh
```

The chain order in `convert-all.sh` is for the 25H2 kit as provisioned. If your checkpoint IDs
differ, find each VM's current disk in its `.vmcx` (`strings -el file.vmcx | grep -i vhdx`) and
its parents with `vhdiinfo` (package `libvhdi-utils`).

### 4. Lab CD, networks, VMs

```bash
Linux/guest/build-iso.sh
Linux/vm/define-lab.sh
```

### 5. First boot: one script, on DC1

Start DC1, GW1 and CM1 (`virt-manager`), sign in to DC1 as `CORP\LabAdmin`, and in an elevated
PowerShell run:

```powershell
powershell -ExecutionPolicy Bypass -File D:\lab-setup.ps1
```

Over PowerShell remoting it runs `server-finish.ps1` on GW1 and CM1, then on DC1:
1. installs the virtio-win guest tools (clipboard, display resize, **QEMU guest agent**);
2. `fix-network.ps1`: removes the ghost Hyper-V adapters, restores static IPs and DNS, sets CM1
   metrics, rebinds GW1's RRAS NAT to the new adapters, and sets DC1's DNS forwarder;
3. `slmgr /rearm`, because the Server 2025 evaluation in the kit had already expired;
4. reboots. Each server logs to `C:\lab-setup.log`.

From then on, `tools/vmexec.py` reaches the servers without anyone signed in:

```bash
sg libvirt -c "python3 Linux/tools/vmexec.py HYD-DC1 'Get-DhcpServerv4Lease -ScopeId 10.0.0.0'"
```

Clients: run `D:\virtio-win-guest-tools.exe` once in each when you use it.

### 6. Optional: pause Windows Update lab-wide

On DC1: `D:\new-update-gpo.ps1` (or through the agent:
`sg libvirt -c "python3 Linux/tools/vmexec.py HYD-DC1" < Linux/guest/new-update-gpo.ps1`).
It creates and links the domain GPO **Lab - Pause Windows Update**: no automatic or internet
updates, but `wuauserv` stays on so CM1's WSUS / Software Update Point can still serve updates.
Unlink it for lab 5.1.

## Lab Manager

```bash
Linux/manager/install.sh          # adds "Lab Manager" to the apps menu
```

A cleaner front end than virt-manager for this lab:
- **Main window:** servers and clients as cards with role, addresses, status and a live CPU
  graph, plus whether the VM's guest tools (QEMU guest agent) are connected. Each card has a
  power button, Connect, and a menu with the power options, Checkpoints… and Open in virt-manager. **Start lab** boots DC1 → GW1 → CM1 → CLIENT1–4, waiting
  for DC1 and GW1 to answer before starting the rest. While it runs, the header shows its progress
  and the button becomes **Cancel**; any VM you start or shut down yourself meanwhile is skipped.
- **Power options, VMware style** (power button on a card, or in the console header):

  | | Option | Does |
  | :--- | :--- | :--- |
  | Safe | **Shut down** | Windows shuts down cleanly (VMware *Shut Down Guest*) |
  | Safe | **Restart** | Windows restarts itself (*Restart Guest*) |
  | Hard | **Power off** | Cuts power instantly, unsaved work lost (*Power Off*); asks first |
  | Hard | **Reset** | Instant reboot without shutting down (*Reset*); asks first |

  Safe actions go through the guest agent when the VM has guest tools, else the virtual power
  button. Windows ignores the power button while it's still starting up, so if the VM is still
  running 90 seconds after Shut down you get a message suggesting to try again or use Power off.
- **Console:** a slim header (VM name, role, Ctrl+Alt+Del, power, full screen) over the VM, which
  resizes to the window. In full screen, touch the top edge for the same controls. Press Ctrl+Alt
  to release the keyboard.

It uses the same SPICE display widget as virt-manager. A VM accepts one viewer at a time, so
opening it in Lab Manager disconnects a virt-manager console showing the same VM. Run one VM's
console directly with `labmanager.py --console HYD-DC1`.

## Checkpoints (the lab guide's "Revert HYD-CLIENT4 to the latest checkpoint")

libvirt can't snapshot a running UEFI VM the way Hyper-V does. With the VM shut down:

```bash
Linux/vm/checkpoint.sh create HYD-CLIENT4 before-lab-5
Linux/vm/checkpoint.sh revert HYD-CLIENT4          # latest, or give a name
Linux/vm/checkpoint.sh list   HYD-CLIENT4
```

## Problems found moving off Hyper-V, and the fixes

| Problem | Cause | Fix (already in these scripts) |
| :--- | :--- | :--- |
| `qemu-img` can't open the disks ("Operation not supported") | QEMU has no differencing-VHDX support | `convert/vhdxflat.py` flattens the chain, reading only allocated blocks (seconds, not hours) |
| `vhdimount` (libvhdi 20210425) output differs | It returns the VHDX header for blocks marked zero/undefined, and numbers views base-first (`vhdiN` = leaf) | Don't convert with it; `verify.py` uses it only as a cross-check on real data blocks |
| GW1 loops into Windows recovery; no crash screen | With VMX visible, Windows starts its own hypervisor, which triple-faults under KVM | VMX is hidden in every VM (`make-vms.py`) |
| Static IPs gone after the move | Hyper-V synthetic NICs became new e1000e adapters | `guest/fix-network.ps1`; fixed MACs tell CorpNet from InterNet NICs |
| GW1 NAT dead | RRAS NAT was bound to the old adapter IDs | `fix-network.ps1` rebinds `Ethernet 2` (public) and `Ethernet` (private) |
| Servers power off about hourly | Server 2025 evaluation expired | `slmgr /rearm` in `server-finish.ps1` (one rearm left in the 25H2 kit; it's now 0) |
| VM display doesn't resize with the window | `vga` has fixed modes; the virtio GPU driver on Windows only offers its own mode list (DC1 stuck at 2048×1536); virt-manager's auto-resize was off | **QXL** graphics in `make-vms.py`: the SPICE agent sets the exact window size through its driver (`qxldod`, from the guest tools). Plus `resize-guest 1`; reopen the console window afterwards. Changing the graphics model needs a full power-off, not a Windows restart |
| New ISO not visible in Windows | Swapping media in place doesn't signal a change | Eject, then insert (see `build-iso.sh`) |
| `virsh` permission denied right after `usermod` | Group membership applies at next login | Log out and back in, or prefix commands with `sg libvirt -c` |

## What's different from Hyper-V

- **No nested virtualization in the VMs** (VMX hidden): Credential Guard (9.4) and Application
  Guard (9.5) need a physical machine. The lab guide already allows that.
- **Fresh software TPMs.** The clients' BitLocker volumes booted without a recovery prompt because
  protection wasn't active yet. If one asks for a key: on DC1,
  `Get-ADObject -Filter 'objectClass -eq "msFVE-RecoveryInformation"' -Properties msFVE-RecoveryPassword`.
- Hyper-V checkpoints aren't carried over, only each VM's current state.
- Autopilot pre-provisioning needs a physical TPM device on either platform.
- Hyper-V Host steps in the lab guide (create a Gen 2 VM, etc.) become virt-manager: Q35,
  UEFI + Secure Boot, TPM 2.0 emulated, SATA disk, e1000e NIC on HYD-CorpNet.
