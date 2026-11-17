#!/usr/bin/env python3
"""Host share for lab VMs: show a folder of this Linux host inside a VM as a drive, like the
local-resources option of a Hyper-V console.

Each VM with a share has a small user service (lab-share@VM) running `labshare.py serve VM`.
It starts a virtiofsd for the configured folder and plugs it into the VM whenever the VM is
running; when the VM goes away it waits for the next start. Nothing is written to the VM's
saved definition except shared memory backing (which virtiofs needs), so a VM always starts
whether or not the share service is running.

virtiofsd runs as the logged-in user inside a user namespace, never as root: the VM sees only
what this account can read, and "read-only" is a read-only mount the guest cannot undo.

A VM with a share plugged in cannot be saved to disk (Freeze lab, Save state), so callers use
unplug() before a save and release() afterwards.

Settings live in Lab Manager's settings.json:  "share": {"HYD-CM1": {"scope": "folder",
"path": "/home/me/LabShare", "readonly": false}}   scope: folder | home | disk
"""
import json
import os
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

URI = 'qemu:///system'
SETTINGS = os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')), 'lab-manager', 'settings.json')
# QEMU is only allowed to open sockets here (AppArmor rule, see the README).
SHARE_DIR = os.environ.get('LAB_SHARE_DIR', os.path.expanduser('~/vms/m365-lab/share'))
VIRTIOFSD = '/usr/libexec/virtiofsd'
TAG = 'hostshare'  # the name Windows shows for the drive
DEFAULT_FOLDER = os.path.expanduser('~/LabShare')
SCOPES = ('folder', 'home', 'disk')
HIDDEN_IN_DISK = ('proc', 'sys', 'dev', 'run', 'tmp')  # live system state, not files worth sharing
UNIT = 'lab-share@{}.service'
SHARED_MEMORY = "<memoryBacking><source type='memfd'/><access mode='shared'/></memoryBacking>"


def log(msg):
    print(msg, flush=True)


# -- settings ---------------------------------------------------------------
def load_config(name):
    """The VM's share settings, or None when it has no share."""
    try:
        with open(SETTINGS) as fh:
            cfg = json.load(fh).get('share', {}).get(name)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or cfg.get('scope') not in SCOPES:
        return None
    return cfg


def save_config(name, cfg):
    """Store (or with None, remove) a VM's share settings, keeping everything else in the file."""
    try:
        with open(SETTINGS) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    shares = data.setdefault('share', {})
    if cfg is None:
        shares.pop(name, None)
    else:
        shares[name] = cfg
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, 'w') as fh:
        json.dump(data, fh, indent=1)


def source_dir(cfg):
    if cfg['scope'] == 'disk':
        return '/'
    if cfg['scope'] == 'home':
        return os.path.expanduser('~')
    return os.path.abspath(os.path.expanduser(cfg.get('path') or DEFAULT_FOLDER))


def describe(cfg):
    if not cfg:
        return 'Off'
    what = {'disk': 'Whole disk', 'home': 'Home folder'}.get(cfg['scope']) or source_dir(cfg)
    return f"{what}, {'read-only' if cfg.get('readonly') else 'read and write'}"


# -- paths and commands -------------------------------------------------------
def socket_path(name):
    return os.path.join(SHARE_DIR, f'{name}.sock')


def mount_point(name):
    return os.path.join(SHARE_DIR, f'{name}.mnt')


def hold_path(name):
    return os.path.join(SHARE_DIR, f'{name}.hold')


def server_script(src, mnt, sock, readonly, hide=()):
    """Shell run inside a fresh user + mount namespace: build the view of the host to share at
    `mnt`, then become virtiofsd. Root in the namespace is the logged-in user outside it."""
    q = lambda s: "'" + s.replace("'", "'\\''") + "'"  # noqa: E731
    lines = ['set -e', 'mount --make-rprivate /',
             f"mount -o rbind{',ro=recursive' if readonly else ''} {q(src)} {q(mnt)}"]
    for sub in hide:
        lines.append(f"[ -d {q(mnt + '/' + sub)} ] && mount -t tmpfs -o ro,size=4k none {q(mnt + '/' + sub)} || true")
    lines.append(f"exec {VIRTIOFSD} --socket-path={q(sock)} --shared-dir={q(mnt)} --sandbox=none")
    return '\n'.join(lines)


def server_command(name, cfg):
    src = source_dir(cfg)
    hide = HIDDEN_IN_DISK if cfg['scope'] == 'disk' else ()
    return ['unshare', '--user', '--map-root-user', '--mount', 'sh', '-c',
            server_script(src, mount_point(name), socket_path(name), bool(cfg.get('readonly')), hide)]


def device_xml(name):
    return (f"<filesystem type='mount'><driver type='virtiofs' queue='1024'/>"
            f"<source socket='{socket_path(name)}'/><target dir='{TAG}'/></filesystem>")


def virsh(*args, timeout=60, stdin=None):
    return subprocess.run(['virsh', '-c', URI, *args], capture_output=True, text=True, timeout=timeout, input=stdin)


# -- VM state -----------------------------------------------------------------
def has_shared_memory(xml):
    """virtiofs needs the VM's memory to be shareable with the virtiofsd process."""
    access = ET.fromstring(xml).find('./memoryBacking/access')
    return access is not None and access.get('mode') == 'shared'


def has_share(xml):
    return any(t.get('dir') == TAG for t in ET.fromstring(xml).findall('./devices/filesystem/target'))


def with_shared_memory(xml):
    """The VM definition with shared memory backing added (unchanged if already there)."""
    root = ET.fromstring(xml)
    if has_shared_memory(xml):
        return xml
    old = root.find('memoryBacking')
    if old is not None:
        root.remove(old)
    root.insert(list(root).index(root.find('memory')) + 1, ET.fromstring(SHARED_MEMORY))
    return ET.tostring(root, encoding='unicode')


def running(name):
    return virsh('domstate', name, timeout=15).stdout.strip() == 'running'


def live_xml(name):
    return virsh('dumpxml', name, timeout=15).stdout


def prepare_vm(name):
    """Give the VM's saved definition shared memory. True if it changed (takes effect at the
    next start from powered off)."""
    xml = virsh('dumpxml', '--inactive', '--security-info', name, timeout=15).stdout
    if has_shared_memory(xml):
        return False
    out = virsh('define', '/dev/stdin', stdin=with_shared_memory(xml))
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or 'could not update the VM definition')
    return True


def status(name):
    """One of: off (no share set), stopped (VM not running), needs-power-off, held, plugged,
    waiting (service should plug it any moment), no-service."""
    if not load_config(name):
        return 'off'
    if not running(name):
        return 'stopped'
    xml = live_xml(name)
    if not has_shared_memory(xml):
        return 'needs-power-off'
    if has_share(xml):
        return 'plugged'
    if os.path.exists(hold_path(name)):
        return 'held'
    active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', UNIT.format(name)]).returncode == 0
    return 'waiting' if active else 'no-service'


# -- plugging -----------------------------------------------------------------
def plug(name):
    out = virsh('attach-device', name, '/dev/stdin', '--live', stdin=device_xml(name))
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())


def unplug(name, wait=20):
    """Take the share out of a running VM and keep the service from putting it back until
    release(). True once it is gone (also when there was none). The guest has to let go of the
    device, so this can time out on a VM whose Windows is not responding."""
    os.makedirs(SHARE_DIR, exist_ok=True)
    open(hold_path(name), 'w').close()
    if not running(name) or not has_share(live_xml(name)):
        return True
    virsh('detach-device', name, '/dev/stdin', '--live', stdin=device_xml(name))
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.5)
        if not running(name) or not has_share(live_xml(name)):
            return True
    return False


def release(name):
    try:
        os.unlink(hold_path(name))
    except OSError:
        pass


# -- service ------------------------------------------------------------------
UNIT_FILE = os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')),
                         'systemd', 'user', 'lab-share@.service')


def unit_text():
    return f"""[Unit]
Description=Lab Manager host share for %i

[Service]
ExecStart=/usr/bin/python3 -B {os.path.realpath(__file__)} serve %i
Restart=on-failure
RestartSec=5
# SIGTERM goes to the service only, so it can unplug the share before virtiofsd is stopped.
KillMode=mixed
TimeoutStopSec=15

[Install]
WantedBy=default.target
"""


def ensure_unit():
    """Write the user service template if it is missing or points at an old copy of this file."""
    text = unit_text()
    try:
        with open(UNIT_FILE) as fh:
            if fh.read() == text:
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(UNIT_FILE), exist_ok=True)
    with open(UNIT_FILE, 'w') as fh:
        fh.write(text)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)


def apply(name, cfg):
    """Save the settings and bring the VM's share service in line. Returns True when the VM
    needs one full power-off before the share can appear."""
    save_config(name, cfg)
    unit = UNIT.format(name)
    if cfg is None:
        subprocess.run(['systemctl', '--user', 'disable', '--now', unit], capture_output=True)
        return False
    if cfg['scope'] == 'folder':
        os.makedirs(source_dir(cfg), exist_ok=True)
    changed = prepare_vm(name)
    ensure_unit()
    subprocess.run(['systemctl', '--user', 'enable', unit], capture_output=True)
    subprocess.run(['systemctl', '--user', 'restart', unit], capture_output=True)
    return changed or (running(name) and not has_shared_memory(live_xml(name)))


def serve(name):
    child = None

    def stop(*_):
        if running(name) and has_share(live_xml(name)):
            virsh('detach-device', name, '/dev/stdin', '--live', stdin=device_xml(name))
            time.sleep(2)
        if child and child.poll() is None:
            child.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    os.makedirs(SHARE_DIR, exist_ok=True)
    told = None
    while True:
        cfg = load_config(name)
        if not cfg:
            log(f'{name}: no share configured, stopping')
            return 0
        src = source_dir(cfg)
        why = None
        if not os.path.isdir(src):
            why = f'folder {src} does not exist'
        elif os.path.exists(hold_path(name)):
            why = 'on hold while the VM is being saved'
        elif not running(name):
            why = 'VM is not running'
        else:
            xml = live_xml(name)
            if not has_shared_memory(xml):
                why = 'VM needs one full power-off before it can take a share'
            elif has_share(xml):
                why = 'a share is already plugged in'
        if why:
            if why != told:
                log(f'{name}: waiting ({why})')
                told = why
            time.sleep(5)
            continue
        told = None
        sock, mnt = socket_path(name), mount_point(name)
        os.makedirs(mnt, exist_ok=True)
        if os.path.exists(sock):
            os.unlink(sock)
        child = subprocess.Popen(server_command(name, cfg))
        for _ in range(50):
            if os.path.exists(sock) or child.poll() is not None:
                break
            time.sleep(0.1)
        if child.poll() is not None or not os.path.exists(sock):
            log(f'{name}: share server did not start')
            child.kill()
            time.sleep(15)
            continue
        os.chmod(sock, 0o666)  # QEMU runs as another user; the folder's AppArmor rule is the gate
        try:
            plug(name)
            log(f'{name}: sharing {describe(cfg)}')
        except Exception as exc:
            log(f'{name}: could not plug the share in: {exc}')
            child.terminate()
            child.wait()
            time.sleep(15)
            continue
        child.wait()  # virtiofsd ends when the VM lets go of it (power-off, unplug, save)
        log(f'{name}: share ended')
        time.sleep(2)


# -- guest --------------------------------------------------------------------
GUEST_SETUP = r"""
$ProgressPreference = 'SilentlyContinue'
$msi = 'C:\Windows\Temp\winfsp.msi'
if (-not (Test-Path 'C:\Program Files (x86)\WinFsp\bin\winfsp-x64.dll')) {
    $p = Start-Process msiexec.exe -ArgumentList '/i', $msi, '/qn', '/norestart' -Wait -PassThru
    "WinFsp installer exit code: $($p.ExitCode)"
} else { 'WinFsp already installed' }
Remove-Item $msi -ErrorAction SilentlyContinue
Set-Service VirtioFsSvc -StartupType Automatic
Start-Service VirtioFsSvc -ErrorAction SilentlyContinue
Get-Service VirtioFsSvc, WinFsp.Launcher | Format-Table Name, Status, StartType -AutoSize | Out-String
"""


def agent(name, cmd, args=None, timeout=30):
    req = {'execute': cmd}
    if args is not None:
        req['arguments'] = args
    out = virsh('qemu-agent-command', name, '--timeout', str(timeout), json.dumps(req), timeout=timeout + 10)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return json.loads(out.stdout).get('return')


def setup_guest(name, msi):
    """Install WinFsp in a Windows VM and set the VirtIO-FS service to start with Windows, through
    the QEMU guest agent (no sign-in needed). The VirtIO-FS driver and service come with the
    virtio-win guest tools; WinFsp is the piece they need to show a drive."""
    import base64
    handle = agent(name, 'guest-file-open', {'path': 'C:\\Windows\\Temp\\winfsp.msi', 'mode': 'wb'})
    with open(msi, 'rb') as fh:
        while chunk := fh.read(48 * 1024):
            agent(name, 'guest-file-write', {'handle': handle, 'buf-b64': base64.b64encode(chunk).decode()})
    agent(name, 'guest-file-close', {'handle': handle})
    script = base64.b64encode(GUEST_SETUP.encode('utf-16-le')).decode()
    pid = agent(name, 'guest-exec', {'path': 'powershell.exe', 'capture-output': True, 'arg': [
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', script]})['pid']
    for _ in range(120):
        st = agent(name, 'guest-exec-status', {'pid': pid})
        if st['exited']:
            out = base64.b64decode(st.get('out-data', '')).decode(errors='replace')
            err = base64.b64decode(st.get('err-data', '')).decode(errors='replace')
            return (out + err).strip()
        time.sleep(2)
    raise RuntimeError('guest setup did not finish in 4 minutes')


def main():
    if len(sys.argv) == 4 and sys.argv[1] == 'setup-guest':
        print(setup_guest(sys.argv[2], sys.argv[3]))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == 'serve':
        return serve(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == 'status':
        print(status(sys.argv[2]), '-', describe(load_config(sys.argv[2])))
        return 0
    print('usage: labshare.py serve|status VM   |   labshare.py setup-guest VM winfsp.msi', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
