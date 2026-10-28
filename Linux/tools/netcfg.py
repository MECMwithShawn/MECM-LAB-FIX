#!/usr/bin/env python3
"""Print static IP config per network adapter from an offline Windows SYSTEM hive."""
import sys

from Registry import Registry

reg = Registry.Registry(sys.argv[1])
cs = 'ControlSet%03d' % reg.open('Select').value('Current').value()
print('computer:', reg.open(f'{cs}\\Control\\ComputerName\\ComputerName').value('ComputerName').value())

names = {}
net_class = reg.open(f'{cs}\\Control\\Network\\{{4D36E972-E325-11CE-BFC1-08002BE10318}}')
for k in net_class.subkeys():
    try:
        names[k.name().lower()] = k.subkey('Connection').value('Name').value()
    except Registry.RegistryKeyNotFoundException:
        pass


def val(key, name):
    try:
        v = key.value(name).value()
        return ','.join(x for x in v if x) if isinstance(v, list) else v
    except Registry.RegistryValueNotFoundException:
        return ''


for k in reg.open(f'{cs}\\Services\\Tcpip\\Parameters\\Interfaces').subkeys():
    dhcp = val(k, 'EnableDHCP')
    ip = val(k, 'IPAddress')
    if not ip and not val(k, 'DhcpIPAddress'):
        continue
    print(f"{names.get(k.name().lower(), k.name())}: dhcp={dhcp} ip={ip or val(k, 'DhcpIPAddress')}"
          f" mask={val(k, 'SubnetMask') or val(k, 'DhcpSubnetMask')} gw={val(k, 'DefaultGateway') or val(k, 'DhcpDefaultGateway')}"
          f" dns={val(k, 'NameServer') or val(k, 'DhcpNameServer')}")
