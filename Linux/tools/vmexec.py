#!/usr/bin/env python3
"""Run PowerShell inside a lab VM through the QEMU guest agent (as SYSTEM, no sign-in needed).

Usage: vmexec.py VM 'powershell commands'     (or the commands on stdin)
Run as a libvirt group member, e.g.: sg libvirt -c "python3 vmexec.py HYD-DC1 'hostname'"
Prints stdout/stderr and exits with the command's exit code.
"""
import base64
import json
import subprocess
import sys
import time

vm = sys.argv[1]
script = sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read()


def agent(cmd, args):
    out = subprocess.run(['virsh', '-c', 'qemu:///system', 'qemu-agent-command', vm, '--timeout', '30',
                          json.dumps({'execute': cmd, 'arguments': args})],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)['return']


# -EncodedCommand takes UTF-16LE base64, which avoids every quoting problem
encoded = base64.b64encode(("$ProgressPreference='SilentlyContinue'\n" + script).encode('utf-16-le')).decode()
pid = agent('guest-exec', {'path': 'powershell.exe', 'capture-output': True,
                           'arg': ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded]})['pid']
while True:
    st = agent('guest-exec-status', {'pid': pid})
    if st['exited']:
        break
    time.sleep(1)
for key, stream in (('out-data', sys.stdout), ('err-data', sys.stderr)):
    if key in st:
        stream.write(base64.b64decode(st[key]).decode('utf-8', 'replace'))
sys.exit(st.get('exitcode', 0))
