#!/usr/bin/env python3
"""Write libvirt domain XML for the hydration lab VMs into xml/. Define with virsh define."""
import os

HERE = os.environ.get('LAB', os.path.expanduser('~/vms/m365-lab'))

# name: (memory GiB, vCPUs, networks, boot order)
VMS = {
    'HYD-DC1': (4, 2, ['HYD-CorpNet'], ['hd']),
    'HYD-GW1': (2, 2, ['HYD-CorpNet', 'HYD-InterNet'], ['hd']),
    'HYD-CM1': (12, 4, ['HYD-CorpNet', 'HYD-InterNet'], ['hd']),
    'HYD-CLIENT1': (4, 2, ['HYD-CorpNet'], ['hd']),
    'HYD-CLIENT2': (4, 2, ['HYD-CorpNet'], ['hd']),
    'HYD-CLIENT3': (4, 2, ['HYD-CorpNet'], ['hd']),
    'HYD-CLIENT4': (4, 2, ['HYD-CorpNet'], ['hd']),
    'HYD-Client5': (4, 2, ['HYD-CorpNet'], ['hd', 'network']),
    'HYD-Client6': (4, 2, ['HYD-CorpNet'], ['hd', 'network']),
}

# Fixed MACs so fix-network.ps1 can tell CorpNet and InterNet adapters apart: <prefix>:<vm index>
MAC = {'HYD-CorpNet': '52:54:00:10:00', 'HYD-InterNet': '52:54:00:16:00'}

TEMPLATE = """<domain type='kvm'>
  <name>{name}</name>
  <metadata>
    <libosinfo:libosinfo xmlns:libosinfo="http://libosinfo.org/xmlns/libvirt/domain/1.0">
      <libosinfo:os id="http://microsoft.com/win/{osid}"/>
    </libosinfo:libosinfo>
  </metadata>
  <memory unit='GiB'>{mem}</memory>
  <vcpu>{cpus}</vcpu>
  <os firmware='efi'>
    <type arch='x86_64' machine='q35'>hvm</type>
    <firmware>
      <feature enabled='yes' name='secure-boot'/>
      <feature enabled='yes' name='enrolled-keys'/>
    </firmware>
{boot}  </os>
  <features>
    <acpi/><apic/>
    <hyperv mode='custom'>
      <relaxed state='on'/><vapic state='on'/><spinlocks state='on' retries='8191'/>
      <vpindex state='on'/><runtime state='on'/><synic state='on'/><stimer state='on'/>
      <frequencies state='on'/><tlbflush state='on'/><ipi state='on'/>
    </hyperv>
    <smm state='on'/>
  </features>
  <cpu mode='host-passthrough' check='none' migratable='on'>
    <topology sockets='1' dies='1' cores='{cpus}' threads='1'/>
    <!-- Hidden: with VMX visible, Windows starts its own hypervisor (VBS / Hyper-V) and that
         triple-faults under this KVM (GW1 reset-looped into WinRE). -->
    <feature policy='disable' name='vmx'/>
  </cpu>
  <clock offset='localtime'>
    <timer name='rtc' tickpolicy='catchup'/><timer name='pit' tickpolicy='delay'/>
    <timer name='hpet' present='no'/><timer name='hypervclock' present='yes'/>
  </clock>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <pm><suspend-to-mem enabled='no'/><suspend-to-disk enabled='no'/></pm>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='none' io='native' discard='unmap'/>
      <source file='{disk}'/>
      <target dev='sda' bus='sata'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{HERE}/lab-fixup.iso'/>
      <target dev='sdb' bus='sata'/>
      <readonly/>
    </disk>
{nics}    <input type='tablet' bus='usb'/>
    <tpm model='tpm-crb'><backend type='emulator' version='2.0'/></tpm>
    <graphics type='spice' autoport='yes'><listen type='address'/><image compression='off'/></graphics>
    <video><model type='qxl' ram='65536' vram='65536' vgamem='16384' heads='1' primary='yes'/></video>
    <channel type='spicevmc'><target type='virtio' name='com.redhat.spice.0'/></channel>
    <channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0'/></channel>
    <memballoon model='none'/>
  </devices>
</domain>
"""

os.makedirs(f'{HERE}/xml', exist_ok=True)
for name, (mem, cpus, nets, boot) in VMS.items():
    idx = list(VMS).index(name) + 1
    nics = ''.join(f"    <interface type='network'><mac address='{MAC[n]}:{idx:02x}'/><source network='{n}'/><model type='e1000e'/></interface>\n" for n in nets)
    xml = TEMPLATE.format(
        name=name, mem=mem, cpus=cpus, nics=nics,
        osid='2k22' if name in ('HYD-DC1', 'HYD-GW1', 'HYD-CM1') else '11',
        boot=''.join(f"    <boot dev='{b}'/>\n" for b in boot),
        disk=f'{HERE}/disks/{name}.qcow2', HERE=HERE)
    with open(f'{HERE}/xml/{name}.xml', 'w') as f:
        f.write(xml)
    print('wrote', name)
