#!/usr/bin/env python3
"""Lab Manager: a Windows 11-style window for the hydration lab VMs (libvirt qemu:///system).

Main window, two views:
- List: VM cards with role, status, guest tools state, live CPU graph, Connect, Checkpoints, and
  VMware-style power options: safe Shut down / Restart (through Windows) and hard Power off / Reset
  (like the physical buttons, confirmed first).
- Consoles: live previews of every VM from libvirt screenshots, which don't take the VM's one SPICE
  viewer slot; click one to open its console.
- Resources: host CPU / memory / storage with the lab's share, the lab's impact in plain words
  (overhead, overcommit, headroom) and a per-VM table of CPU, host RAM, disk and network use.
"Start lab" boots DC1 -> GW1 -> CM1 -> CLIENT1-4 in order,
shows its progress, can be cancelled, and skips any VM you start or stop yourself meanwhile.
Console: a slim header (VM name, Ctrl+Alt+Del, resolution, power, full screen) over the SPICE
display. Resolution "Auto" resizes the guest to the window; a fixed size is scaled to fit. Both need
QXL graphics + the SPICE agent in the guest. Choices are kept per VM in ~/.config/lab-manager.

Run: python3 labmanager.py            (or: labmanager.py --console HYD-DC1)
Needs: python3-gi, gir1.2-spiceclientgtk-3.0, python3-libvirt (all pulled in by virt-manager),
qemu-utils for checkpoints, and membership of the libvirt group.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('SpiceClientGtk', '3.0')
gi.require_version('SpiceClientGLib', '2.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango, SpiceClientGLib, SpiceClientGtk  # noqa: E402
import libvirt  # noqa: E402

URI = 'qemu:///system'
APP_ID = 'io.github.mecmwithshawn.LabManager'
ACCENT = (0.376, 0.804, 1.0)  # #60CDFF, the Windows 11 dark-mode accent

# name: (kind, role, addresses)
LAB = {
    'HYD-DC1': ('server', 'Domain controller · DNS · DHCP', '10.0.0.6'),
    'HYD-GW1': ('server', 'Internet gateway (RRAS NAT)', '10.0.0.254 · 192.168.16.254'),
    'HYD-CM1': ('server', 'Configuration Manager · WSUS · WDS', '10.0.0.7 · 192.168.16.7'),
    'HYD-CLIENT1': ('client', 'Windows 11 · domain joined', 'DHCP'),
    'HYD-CLIENT2': ('client', 'Windows 11 · domain joined', 'DHCP'),
    'HYD-CLIENT3': ('client', 'Windows 11', 'DHCP'),
    'HYD-CLIENT4': ('client', 'Windows 11', 'DHCP'),
    'HYD-Client5': ('client', 'Bare metal · PXE / OSD', 'DHCP'),
    'HYD-Client6': ('client', 'Bare metal · PXE / OSD', 'DHCP'),
}
RESOLUTIONS = [(1280, 720), (1366, 768), (1600, 900), (1920, 1080), (2560, 1440)]
SETTINGS = os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')), 'lab-manager', 'settings.json')


def load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, SETTINGS)


START_ORDER = ['HYD-DC1', 'HYD-GW1', 'HYD-CM1', 'HYD-CLIENT1', 'HYD-CLIENT2', 'HYD-CLIENT3', 'HYD-CLIENT4']

STATE_TEXT = {
    libvirt.VIR_DOMAIN_RUNNING: ('Running', 'running'),
    libvirt.VIR_DOMAIN_PAUSED: ('Paused', 'paused'),
    libvirt.VIR_DOMAIN_SHUTDOWN: ('Shutting down', 'busy'),
    libvirt.VIR_DOMAIN_SHUTOFF: ('Off', 'off'),
    libvirt.VIR_DOMAIN_CRASHED: ('Crashed', 'error'),
    libvirt.VIR_DOMAIN_PMSUSPENDED: ('Suspended', 'paused'),
    'saved': ('Saved', 'saved'),  # shut off with a managed save image: Start resumes it
}

CSS = b"""
window.lab, window.lab-console { background-color: #202020; }
.lab headerbar, .lab-console headerbar {
    background: #1b1b1b; border: none; border-bottom: 1px solid rgba(255,255,255,0.06);
    box-shadow: none; min-height: 40px; padding: 0 4px 0 12px;
}
.lab headerbar .title, .lab-console headerbar .title { font-weight: 600; font-size: 13px; }
.lab headerbar .subtitle, .lab-console headerbar .subtitle { font-size: 11px; color: #9a9a9a; }
.section { font-weight: 600; font-size: 13px; color: #d0d0d0; margin: 18px 2px 8px 2px; }
.card {
    background: #2b2b2b; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);
    padding: 10px 12px; margin-bottom: 6px;
}
.card:hover { background: #303030; }
.vm-name { font-weight: 600; font-size: 14px; color: #ffffff; }
.vm-role { font-size: 12px; color: #a8a8a8; }
.vm-addr { font-size: 11px; color: #8a8a8a; font-feature-settings: "tnum"; }
.pill { border-radius: 999px; padding: 1px 10px; font-size: 11px; font-weight: 600; }
.pill.running { background: rgba(108,203,95,0.16); color: #6ccb5f; }
.pill.off { background: rgba(255,255,255,0.07); color: #9e9e9e; }
.pill.busy, .pill.paused { background: rgba(252,225,0,0.14); color: #fce100; }
.pill.error { background: rgba(255,153,164,0.16); color: #ff99a4; }
.pill.saved { background: rgba(176,150,255,0.18); color: #c3b1ff; }
.cpu { font-size: 11px; color: #9a9a9a; font-feature-settings: "tnum"; }
button.icon {
    background: none; border: none; box-shadow: none; border-radius: 6px;
    min-width: 30px; min-height: 30px; padding: 0; color: #e0e0e0;
}
button.icon:hover { background: rgba(255,255,255,0.08); }
button.icon:disabled { color: rgba(255,255,255,0.25); }
button.accent {
    background: #60cdff; color: #003a57; border: none; box-shadow: none; border-radius: 6px;
    font-weight: 600; padding: 4px 14px; min-height: 28px;
}
button.accent:hover { background: #5abce9; }
button.accent:disabled { background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.35); }
button.subtle {
    background: rgba(255,255,255,0.06); color: #f0f0f0; border: 1px solid rgba(255,255,255,0.06);
    box-shadow: none; border-radius: 6px; padding: 4px 12px; min-height: 28px;
}
button.subtle:hover { background: rgba(255,255,255,0.10); }
popover.background {
    background-color: #2c2c2c; color: #f2f2f2; border-radius: 8px;
    border: 1px solid rgba(255,255,255,0.08); box-shadow: 0 8px 24px rgba(0,0,0,0.45);
}
popover separator { background-color: rgba(255,255,255,0.08); }
.menu-item { padding: 6px 10px; border-radius: 4px; min-width: 230px; }
.menu-item:hover { background: rgba(255,255,255,0.08); }
.menu-title { font-size: 13px; color: #f2f2f2; }
.menu-item:disabled .menu-title { color: rgba(255,255,255,0.35); }
.menu-desc { font-size: 11px; color: #9a9a9a; }
.menu-item:disabled .menu-desc { color: rgba(255,255,255,0.25); }
.menu-header { font-size: 11px; font-weight: 600; color: #8a8a8a; margin: 6px 10px 2px 10px; }
.tools-on { font-size: 11px; color: #60cdff; }
.tools-off { font-size: 11px; color: #8a8a8a; }
.toast {
    background: #2d2d2d; color: #f2f2f2; border-radius: 8px; padding: 10px 16px;
    border: 1px solid rgba(255,255,255,0.08); margin: 16px;
}
.placeholder { color: #9a9a9a; font-size: 14px; }
.placeholder-title { color: #ffffff; font-size: 18px; font-weight: 600; }
button.res { font-feature-settings: "tnum"; }
.menu-item.checked .menu-title { color: #60cdff; font-weight: 600; }
.tile { background: #2b2b2b; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06); padding: 8px; }
.tile:hover { background: #333333; border-color: rgba(96,205,255,0.45); }
.tile-screen { background: #111111; border-radius: 5px; }
.tile-name { font-weight: 600; font-size: 13px; color: #ffffff; }
.tile-off { color: #8a8a8a; font-size: 12px; }
stackswitcher button { padding: 2px 14px; min-height: 26px; }
.res-head { font-size: 11px; font-weight: 600; color: #8a8a8a; margin-bottom: 4px; }
.res-cell { padding: 5px 0; border-bottom: 1px solid rgba(255,255,255,0.05); }
.res-main { font-size: 13px; color: #f0f0f0; font-feature-settings: "tnum"; }
.res-total { font-size: 13px; color: #60cdff; font-weight: 600; font-feature-settings: "tnum"; }
.res-sub { font-size: 11px; color: #9a9a9a; font-feature-settings: "tnum"; }
.res-big { font-size: 20px; font-weight: 600; color: #ffffff; font-feature-settings: "tnum"; }
.impact { font-size: 12px; color: #d8d8d8; }
.fs-bar { background: rgba(32,32,32,0.92); border-radius: 0 0 8px 8px; padding: 4px 8px; }
"""


GLYPHS = {
    'server': '''<rect x="11" y="9" width="18" height="9" rx="2" fill="none" stroke="{fg}" stroke-width="1.8"/>
<rect x="11" y="22" width="18" height="9" rx="2" fill="none" stroke="{fg}" stroke-width="1.8"/>
<circle cx="15" cy="13.5" r="1.3" fill="{fg}"/><circle cx="15" cy="26.5" r="1.3" fill="{fg}"/>
<path d="M19 13.5h6M19 26.5h6" stroke="{fg}" stroke-width="1.6" stroke-linecap="round"/>''',
    'client': '''<rect x="9" y="10" width="22" height="15" rx="2.5" fill="none" stroke="{fg}" stroke-width="1.8"/>
<path d="M17 29.5h6M20 25v4.5" stroke="{fg}" stroke-width="1.8" stroke-linecap="round"/>''',
}


# Frozen (saved state) tile: ice glass, frosted rim, icicles and a snowflake badge.
# Six-armed snowflake centred on 0,0 (radius ~5), each arm with a small chevron
FLAKE = ''.join(
    f'<g transform="rotate({a})"><path d="M0 0V-4.6M-1.5 -3.4L0 -2.2L1.5 -3.4" fill="none" stroke="{{c}}" '
    f'stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/></g>' for a in range(0, 360, 60))

FROZEN_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 40 40">
<defs>
  <linearGradient id="ice" x1="0" y1="0" x2="0.35" y2="1">
    <stop offset="0" stop-color="#c9efff" stop-opacity="0.55"/>
    <stop offset="0.5" stop-color="#62c3ff" stop-opacity="0.32"/>
    <stop offset="1" stop-color="#2f86d8" stop-opacity="0.42"/>
  </linearGradient>
  <linearGradient id="rim" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#ffffff" stop-opacity="0.85"/>
    <stop offset="0.5" stop-color="#a8e6ff" stop-opacity="0.45"/>
    <stop offset="1" stop-color="#6cc6ff" stop-opacity="0.65"/>
  </linearGradient>
  <linearGradient id="glyph" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#f2fbff"/><stop offset="1" stop-color="#9fdcff"/>
  </linearGradient>
  <radialGradient id="badge" cx="0.35" cy="0.3" r="0.8">
    <stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#cbeeff"/>
  </radialGradient>
</defs>
<rect x="0.5" y="0.5" width="39" height="39" rx="9" fill="url(#ice)" stroke="url(#rim)" stroke-width="1"/>
<!-- frost sheen along the top-left, like a glazed surface -->
<path d="M3 13 Q4 4 13 3 L19 3 Q8 6 5 17 Z" fill="#ffffff" fill-opacity="0.16"/>
<!-- icicles hanging from the top edge -->
<path d="M9 0.8 l1.3 4.2 l1.3 -4.2 Z M24 0.8 l1 3.2 l1 -3.2 Z M30 0.8 l1.4 5 l1.4 -5 Z" fill="#e8f8ff" fill-opacity="0.75"/>
{glyph}
<!-- faint frost crack across the glyph -->
<path d="M8 33 L13 28 L12 25 L16 21" fill="none" stroke="#ffffff" stroke-opacity="0.35" stroke-width="0.7"/>
<!-- sparkles -->
<path d="M34 12 l0.6 1.6 l1.6 0.6 l-1.6 0.6 l-0.6 1.6 l-0.6 -1.6 l-1.6 -0.6 l1.6 -0.6 Z" fill="#ffffff" fill-opacity="0.9"/>
<circle cx="6" cy="27" r="0.7" fill="#ffffff" fill-opacity="0.8"/>
<!-- snowflake badge, bottom right -->
<circle cx="32" cy="32" r="6.2" fill="url(#badge)" stroke="#5fbff7" stroke-width="0.9"/>
<g transform="translate(32 32) scale(0.92)">{flake}</g>
</svg>'''




# Running and off tiles in the same glass style: running = lit blue with a green "on" badge,
# off = dark slate with a grey power badge.
STATE_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 40 40">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0.35" y2="1">
    <stop offset="0" stop-color="{bg0}" stop-opacity="{bgo0}"/>
    <stop offset="1" stop-color="{bg1}" stop-opacity="{bgo1}"/>
  </linearGradient>
  <linearGradient id="rim" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{rim0}" stop-opacity="{rimo0}"/>
    <stop offset="1" stop-color="{rim1}" stop-opacity="{rimo1}"/>
  </linearGradient>
  <linearGradient id="glyph" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="{g0}"/><stop offset="1" stop-color="{g1}"/>
  </linearGradient>
  <radialGradient id="glow" cx="0.5" cy="0.45" r="0.6">
    <stop offset="0" stop-color="{glow}" stop-opacity="{glowo}"/><stop offset="1" stop-color="{glow}" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="badge" cx="0.35" cy="0.3" r="0.8">
    <stop offset="0" stop-color="{b0}"/><stop offset="1" stop-color="{b1}"/>
  </radialGradient>
</defs>
<rect x="0.5" y="0.5" width="39" height="39" rx="9" fill="url(#bg)" stroke="url(#rim)" stroke-width="1"/>
<rect x="0.5" y="0.5" width="39" height="39" rx="9" fill="url(#glow)"/>
<path d="M3 13 Q4 4 13 3 L19 3 Q8 6 5 17 Z" fill="#ffffff" fill-opacity="{sheen}"/>
{glyph}
<circle cx="32" cy="32" r="6.2" fill="url(#badge)" stroke="{bstroke}" stroke-width="0.9"/>
<g transform="translate(32 32)">{badge}</g>
</svg>'''

# running: a lit "on" dot with a pulse ring
RUN_BADGE = ('<circle r="4.3" fill="none" stroke="#ffffff" stroke-opacity="0.75" stroke-width="0.9"/>'
             '<circle r="2.4" fill="#ffffff"/>')
# off: a power symbol
OFF_BADGE = ('<path d="M-2.6 -2.2 A3.5 3.5 0 1 0 2.6 -2.2" fill="none" stroke="#d9dee4" stroke-width="1.35" '
             'stroke-linecap="round"/><path d="M0 -4.2 V-0.4" stroke="#d9dee4" stroke-width="1.35" stroke-linecap="round"/>')

STYLES = {
    'running': dict(bg0='#2fa8ff', bgo0=0.42, bg1='#0a3f8f', bgo1=0.62, rim0='#8fdcff', rimo0=0.9,
                    rim1='#1f86e8', rimo1=0.8, g0='#ffffff', g1='#7fd8ff', glow='#3fb8ff', glowo=0.5,
                    sheen=0.14, b0='#7ff0a0', b1='#1fae55', bstroke='#0f7a3a', badge=RUN_BADGE),
    'off': dict(bg0='#9aa6b2', bgo0=0.14, bg1='#3a4048', bgo1=0.30, rim0='#ffffff', rimo0=0.22,
                rim1='#ffffff', rimo1=0.08, g0='#a9b1ba', g1='#6f7882', glow='#000000', glowo=0.0,
                sheen=0.06, b0='#6c747d', b1='#3d434a', bstroke='#2a2f35', badge=OFF_BADGE),
}



def tile_pixbuf(kind, active, frozen=False, size=40):
    if frozen:
        svg = FROZEN_SVG.format(size=size, glyph=GLYPHS[kind].format(fg='url(#glyph)'), flake=FLAKE.format(c='#1f8fe0'))
    else:
        svg = STATE_SVG.format(size=size, glyph=GLYPHS[kind].format(fg='url(#glyph)'),
                               **STYLES['running' if active else 'off'])
    loader = GdkPixbuf.PixbufLoader.new_with_type('svg')
    loader.write(svg.encode())
    loader.close()
    return loader.get_pixbuf()


def connect():
    try:
        return libvirt.open(URI)
    except libvirt.libvirtError as e:
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                text="Can't connect to the lab's VMs")
        dlg.format_secondary_text(f'{e}\n\nIf this says permission denied, log out and back in '
                                  'once so your libvirt group membership takes effect.')
        dlg.run()
        sys.exit(1)


def run_bg(work, done=None):
    """Run work() on a thread; call done(result, error) back on the GTK thread."""
    def target():
        try:
            res, err = work(), None
        except Exception as e:  # shown to the user in a toast
            res, err = None, e
        if done:
            GLib.idle_add(done, res, err)
    threading.Thread(target=target, daemon=True).start()


def icon_button(icon, tooltip, cb=None):
    b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
    b.get_style_context().add_class('icon')
    b.set_tooltip_text(tooltip)
    b.set_valign(Gtk.Align.CENTER)
    if cb:
        b.connect('clicked', lambda *_: cb())
    return b


def set_icon(button, icon):
    button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))


def text_button(label, style, cb=None):
    b = Gtk.Button(label=label)
    b.get_style_context().add_class(style)
    b.set_valign(Gtk.Align.CENTER)
    if cb:
        b.connect('clicked', lambda *_: cb())
    return b


def popover_menu(relative_to, items):
    """items: (label, callback, sensitive[, description]), ('header', text), or None for a separator."""
    pop = Gtk.Popover.new(relative_to)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=6, spacing=1)
    for item in items:
        if item is None:
            box.pack_start(Gtk.Separator(margin_top=4, margin_bottom=4), False, False, 0)
            continue
        if item[0] == 'header':
            h = Gtk.Label(label=item[1], xalign=0)
            h.get_style_context().add_class('menu-header')
            box.pack_start(h, False, False, 0)
            continue
        label, cb, sensitive = item[:3]
        desc = item[3] if len(item) > 3 else None
        b = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
        b.get_style_context().add_class('menu-item')
        if len(item) > 4 and item[4]:
            b.get_style_context().add_class('checked')
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        t = Gtk.Label(label=label, xalign=0)
        t.get_style_context().add_class('menu-title')
        inner.pack_start(t, False, False, 0)
        if desc:
            d = Gtk.Label(label=desc, xalign=0)
            d.get_style_context().add_class('menu-desc')
            inner.pack_start(d, False, False, 0)
        b.add(inner)
        b.set_sensitive(sensitive)
        b.connect('clicked', lambda _b, c=cb: (pop.popdown(), c()))
        box.pack_start(b, False, False, 0)
    box.show_all()
    pop.add(box)
    return pop


class Toaster:
    """A small toast at the bottom of a window's overlay."""
    def __init__(self, overlay):
        self.rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
                                halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
        self.label = Gtk.Label(wrap=True, max_width_chars=70)
        self.label.get_style_context().add_class('toast')
        self.rev.add(self.label)
        overlay.add_overlay(self.rev)
        self._timer = None

    def show(self, text, seconds=4):
        self.label.set_text(text)
        self.rev.set_reveal_child(True)
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add_seconds(seconds, self._hide)

    def _hide(self):
        self.rev.set_reveal_child(False)
        self._timer = None
        return False


class Sparkline(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_size_request(96, 28)
        self.values = [0.0] * 40
        self.connect('draw', self._draw)

    def push(self, v):
        self.values = self.values[1:] + [max(0.0, min(100.0, v))]
        self.queue_draw()

    def clear(self):
        self.values = [0.0] * len(self.values)
        self.queue_draw()

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        n = len(self.values)
        pts = [(i * w / (n - 1), h - 2 - (v / 100.0) * (h - 4)) for i, v in enumerate(self.values)]
        cr.move_to(0, h)
        for x, y in pts:
            cr.line_to(x, y)
        cr.line_to(w, h)
        cr.close_path()
        cr.set_source_rgba(*ACCENT, 0.16)
        cr.fill()
        cr.move_to(*pts[0])
        for x, y in pts[1:]:
            cr.line_to(x, y)
        cr.set_source_rgba(*ACCENT, 0.9)
        cr.set_line_width(1.4)
        cr.stroke()
        return False


def disk_path(dom):
    root = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
    for d in root.findall("./devices/disk[@device='disk']/source"):
        return d.get('file')
    raise RuntimeError('no disk found')


def list_checkpoints(dom):
    out = subprocess.run(['qemu-img', 'snapshot', '-U', '-l', disk_path(dom)],
                         capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 5:
            rows.append((parts[1], f'{parts[-4]} {parts[-3]}' if re.match(r'\d{4}-', parts[-4]) else ''))
    return rows


class VmRow(Gtk.Box):
    def __init__(self, app, dom):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.app, self.dom, self.name = app, dom, dom.name()
        kind, role, addr = LAB.get(self.name, ('client', '', ''))
        self.get_style_context().add_class('card')
        self.last = None  # (cpu_time_ns, wallclock_ns)

        self.kind = kind
        self.icon = Gtk.Image.new_from_pixbuf(tile_pixbuf(kind, False))
        self.icon.set_valign(Gtk.Align.CENTER)
        self.pack_start(self.icon, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=8)
        name = Gtk.Label(label=self.name.replace('HYD-', ''), xalign=0)
        name.get_style_context().add_class('vm-name')
        self.pill = Gtk.Label()
        self.pill.get_style_context().add_class('pill')
        self.pill.set_valign(Gtk.Align.CENTER)
        top.pack_start(name, False, False, 0)
        top.pack_start(self.pill, False, False, 0)
        r = Gtk.Label(label=role, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        r.get_style_context().add_class('vm-role')
        bottom = Gtk.Box(spacing=8)
        a = Gtk.Label(label=addr, xalign=0)
        a.get_style_context().add_class('vm-addr')
        self.tools = Gtk.Label(xalign=0)
        bottom.pack_start(a, False, False, 0)
        bottom.pack_start(self.tools, False, False, 0)
        for wdg in (top, r, bottom):
            text.pack_start(wdg, False, False, 0)
        self.pack_start(text, True, True, 0)

        cpu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.spark = Sparkline()
        self.cpu_label = Gtk.Label(xalign=1)
        self.cpu_label.get_style_context().add_class('cpu')
        cpu_box.pack_start(self.spark, False, False, 0)
        cpu_box.pack_start(self.cpu_label, False, False, 0)
        self.pack_start(cpu_box, False, False, 6)

        self.power = icon_button('media-playback-start-symbolic', 'Start', self.toggle_power)
        self.connect_btn = text_button('Connect', 'subtle', lambda: app.open_console(self.name))
        self.more = icon_button('view-more-symbolic', 'More')
        self.more.connect('clicked', lambda *_: self.show_menu())
        for wdg in (self.power, self.connect_btn, self.more):
            self.pack_start(wdg, False, False, 0)
        self.state = None
        self.key = None  # state, or 'saved' when it's off with a saved memory image
        self.agent = None
        self._xml_tick = 0
        self.refresh()

    def refresh_tools(self):
        """Guest tools = the QEMU guest agent channel libvirt reports as connected."""
        try:
            root = ET.fromstring(self.dom.XMLDesc(0))
            t = root.find("./devices/channel/target[@name='org.qemu.guest_agent.0']")
            agent = t is not None and t.get('state') == 'connected'
        except (libvirt.libvirtError, ET.ParseError):
            return
        self.agent = agent
        ctx = self.tools.get_style_context()
        ctx.remove_class('tools-on')
        ctx.remove_class('tools-off')
        if not self.running:
            self.tools.set_text('')
            return
        ctx.add_class('tools-on' if agent else 'tools-off')
        self.tools.set_text('· Guest tools connected' if agent else '· No guest tools: safe shutdown uses the power button')

    def refresh(self):
        try:
            state, _max, mem, vcpus, cpu_time = self.dom.info()
        except libvirt.libvirtError:
            return
        saved = state == libvirt.VIR_DOMAIN_SHUTOFF and self.dom.hasManagedSaveImage(0)
        key = 'saved' if saved else state
        text, cls = STATE_TEXT.get(key, ('Unknown', 'off'))
        if key != self.key:
            self.key = key
            ctx = self.pill.get_style_context()
            for c in ('running', 'off', 'busy', 'paused', 'error', 'saved'):
                ctx.remove_class(c)
            ctx.add_class(cls)
            self.pill.set_text(text)
            self.icon.set_from_pixbuf(tile_pixbuf(self.kind, key == libvirt.VIR_DOMAIN_RUNNING, frozen=key == 'saved'))
            self.icon.set_tooltip_text('Frozen: memory saved to disk; Resume continues right here' if key == 'saved' else None)
        if state != self.state:
            running = state == libvirt.VIR_DOMAIN_RUNNING
            set_icon(self.power, 'system-shutdown-symbolic' if running else 'media-playback-start-symbolic')
            self.power.set_tooltip_text('Power options' if running else 'Start')
            self.spark.set_opacity(1.0 if running else 0.0)
            self.state = state
            if not running:
                self.spark.clear()
                self.last = None
            self._xml_tick = 0
        self._xml_tick -= 1
        if self._xml_tick <= 0:  # every ~10 s, and right after a state change
            self.refresh_tools()
            self._xml_tick = 5
        mem_gb = mem / 1048576
        if state == libvirt.VIR_DOMAIN_RUNNING:
            now = time.monotonic_ns()
            if self.last:
                pct = min(100.0, (cpu_time - self.last[0]) / max(1, (now - self.last[1]) * vcpus) * 100)
                self.spark.push(pct)
                self.cpu_label.set_text(f'{pct:4.0f}% CPU · {mem_gb:g} GB')
            self.last = (cpu_time, now)
        else:
            self.cpu_label.set_text(f'{vcpus} vCPU · {mem_gb:g} GB')

    @property
    def running(self):
        return self.state == libvirt.VIR_DOMAIN_RUNNING

    def toggle_power(self):
        if self.state == libvirt.VIR_DOMAIN_SHUTOFF:
            self.app.start_vm(self.name)
        else:
            popover_menu(self.power, self.app.power_items(self.name, self.app.window)).popup()

    def show_menu(self):
        items = self.app.power_items(self.name, self.app.window) + [
            None,
            ('Checkpoints…', lambda: CheckpointDialog(self.app.window, self.dom, self.state).run_dialog(), True),
            ('Open in virt-manager', lambda: subprocess.Popen(
                ['virt-manager', '--connect', URI, '--show-domain-console', self.name]), True),
        ]
        popover_menu(self.more, items).popup()


class CheckpointDialog(Gtk.Dialog):
    def __init__(self, parent, dom, state):
        super().__init__(title=f'Checkpoints · {dom.name()}', transient_for=parent, modal=True)
        self.dom = dom
        self.off = state == libvirt.VIR_DOMAIN_SHUTOFF
        self.set_default_size(460, 340)
        self.get_style_context().add_class('lab')
        box = self.get_content_area()
        box.set_spacing(10)
        box.set_border_width(14)
        note = Gtk.Label(xalign=0, wrap=True)
        note.set_markup('Checkpoints are saved inside the VM\'s disk.' + (
            '' if self.off else '\n<b>Shut the VM down first</b> to create or revert one.'))
        box.pack_start(note, False, False, 0)
        self.store = Gtk.ListStore(str, str)
        view = Gtk.TreeView(model=self.store, headers_visible=True)
        for i, title in enumerate(('Name', 'Created')):
            view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i))
        self.view = view
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(view)
        box.pack_start(sw, True, True, 0)
        row = Gtk.Box(spacing=8)
        self.entry = Gtk.Entry(placeholder_text='Name (optional)')
        row.pack_start(self.entry, True, True, 0)
        for label, style, cb in (('Create', 'accent', self.create), ('Revert', 'subtle', self.revert),
                                 ('Delete', 'subtle', self.delete)):
            b = text_button(label, style, cb)
            b.set_sensitive(self.off)
            row.pack_start(b, False, False, 0)
        box.pack_start(row, False, False, 0)
        self.add_button('Close', Gtk.ResponseType.CLOSE)
        self.reload()

    def reload(self):
        self.store.clear()
        try:
            for name, created in list_checkpoints(self.dom):
                self.store.append([name, created])
        except Exception as e:
            self.store.append([f'error: {e}', ''])

    def selected(self):
        model, it = self.view.get_selection().get_selected()
        return model[it][0] if it else None

    def _img(self, *args):
        subprocess.run(['qemu-img', 'snapshot', *args, disk_path(self.dom)], capture_output=True, text=True, check=True)
        self.reload()

    def create(self):
        self._img('-c', self.entry.get_text().strip() or time.strftime('%Y-%m-%d_%H%M'))
        self.entry.set_text('')

    def revert(self):
        name = self.selected() or (self.store[len(self.store) - 1][0] if len(self.store) else None)
        if name and self.confirm(f'Revert {self.dom.name()} to "{name}"?', 'Changes since that checkpoint are lost.'):
            self._img('-a', name)

    def delete(self):
        name = self.selected()
        if name and self.confirm(f'Delete checkpoint "{name}"?', 'This can\'t be undone.'):
            self._img('-d', name)

    def confirm(self, text, secondary):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL, text=text)
        dlg.format_secondary_text(secondary)
        ok = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        return ok

    def run_dialog(self):
        self.show_all()
        self.run()
        self.destroy()


class Console(Gtk.Window):
    """Slim viewer: header with name + a few actions, SPICE display filling the rest."""
    def __init__(self, app, dom):
        super().__init__(title=dom.name())
        self.app, self.dom, self.name = app, dom, dom.name()
        self.get_style_context().add_class('lab-console')
        self.set_default_size(1280, 800)
        self.set_icon_name('computer')
        self.session = self.display = self.main = None
        self.qxl = False          # live graphics model can change mode (QXL); others ignore requests
        self.guest_size = None    # the guest's current screen size, from the display channel
        self.auto_connect = True  # off after we lose a connection, so two viewers don't keep kicking each other
        self.is_fullscreen = False
        self.resolution = load_settings().get('resolution', {}).get(self.name)  # None = Auto, else [w, h]

        hb = Gtk.HeaderBar(show_close_button=True, title=self.name.replace('HYD-', ''))
        hb.set_subtitle(LAB.get(self.name, ('', '', ''))[1])
        self.cad = text_button('Ctrl+Alt+Del', 'subtle', self.send_cad)
        hb.pack_start(self.cad)
        self.fs_btn = icon_button('view-fullscreen-symbolic', 'Full screen', self.toggle_fullscreen)
        self.power_btn = icon_button('system-shutdown-symbolic', 'Power')
        self.power_btn.connect('clicked', lambda *_: self.power_menu(self.power_btn))
        hb.pack_end(self.fs_btn)
        hb.pack_end(self.power_btn)
        self.res_btn = Gtk.Button()
        self.res_btn.get_style_context().add_class('subtle')
        self.res_btn.get_style_context().add_class('res')
        self.res_btn.set_valign(Gtk.Align.CENTER)
        self.res_btn.connect('clicked', lambda *_: self.resolution_menu(self.res_btn))
        hb.pack_end(self.res_btn)
        self.update_res_button()
        self.set_titlebar(hb)

        self.overlay = Gtk.Overlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.overlay.add(self.stack)
        self.toast = Toaster(self.overlay)
        self.add(self.overlay)

        # Off / connecting placeholder
        ph = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.ph_title = Gtk.Label()
        self.ph_title.get_style_context().add_class('placeholder-title')
        self.ph_text = Gtk.Label()
        self.ph_text.get_style_context().add_class('placeholder')
        self.ph_start = text_button('Start', 'accent', self.start_vm)
        self.ph_start.set_halign(Gtk.Align.CENTER)
        self.ph_reconnect = text_button('Reconnect', 'accent', self.reconnect)
        self.ph_reconnect.set_halign(Gtk.Align.CENTER)
        for w in (self.ph_title, self.ph_text, self.ph_start, self.ph_reconnect):
            ph.pack_start(w, False, False, 0)
        self.stack.add_named(ph, 'placeholder')

        # Full-screen bar that drops down when the pointer touches the top edge
        self.fs_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                   halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        bar = Gtk.Box(spacing=4)
        bar.get_style_context().add_class('fs-bar')
        bar.pack_start(Gtk.Label(label=self.name.replace('HYD-', ''), margin_start=6, margin_end=8), False, False, 0)
        bar.pack_start(text_button('Ctrl+Alt+Del', 'subtle', self.send_cad), False, False, 0)
        fs_power = icon_button('system-shutdown-symbolic', 'Power')
        fs_power.connect('clicked', lambda *_: self.power_menu(fs_power))
        bar.pack_start(fs_power, False, False, 0)
        bar.pack_start(icon_button('view-restore-symbolic', 'Leave full screen', self.toggle_fullscreen), False, False, 0)
        self.fs_rev.add(bar)
        self.overlay.add_overlay(self.fs_rev)

        self.connect('destroy', self.on_destroy)
        self.connect('window-state-event', self.on_window_state)
        self.connect('notify::scale-factor', self.on_scale_factor)
        self._poll = GLib.timeout_add(1000, self.tick)
        self.show_all()
        self.ph_reconnect.hide()
        self.tick()

    # -- connection -------------------------------------------------------
    def spice_uri(self):
        root = ET.fromstring(self.dom.XMLDesc(0))
        g = root.find("./devices/graphics[@type='spice']")
        port = g.get('port') if g is not None else None
        if not port or port == '-1':
            raise RuntimeError('no SPICE display')
        host = g.get('listen') or '127.0.0.1'
        return f'spice://{host}:{port}'

    def tick(self):
        try:
            state = self.dom.info()[0]
        except libvirt.libvirtError:
            return True
        running = state == libvirt.VIR_DOMAIN_RUNNING
        cad = running and self.display is not None
        if self.cad.get_sensitive() != cad:
            self.cad.set_sensitive(cad)
        self.update_res_button()
        if running and self.session is None and self.auto_connect:
            self.open_session()
        elif not running:
            self.auto_connect = True  # connect by itself once it's started again
            self.ph_reconnect.hide()
            if self.session:
                self.close_session()
            saved = state == libvirt.VIR_DOMAIN_SHUTOFF and self.dom.hasManagedSaveImage(0)
            text, _ = STATE_TEXT.get('saved' if saved else state, ('Off', 'off'))
            self.ph_title.set_text(f'{self.name.replace("HYD-", "")} is {text.lower()}')
            self.ph_text.set_text('Resume it to continue where it left off.' if saved else
                                  'Start it to connect.' if state == libvirt.VIR_DOMAIN_SHUTOFF else '')
            self.ph_start.set_label('Resume' if saved else 'Start')
            self.ph_start.set_visible(state == libvirt.VIR_DOMAIN_SHUTOFF)
            self.stack.set_visible_child_name('placeholder')
        if self.is_fullscreen:
            self.check_top_edge()
        return True

    def open_session(self):
        try:
            uri = self.spice_uri()
        except Exception as e:
            self.ph_title.set_text('Connecting…')
            self.ph_text.set_text(str(e))
            return
        self.ph_title.set_text('Connecting…')
        self.ph_text.set_text('')
        self.ph_start.hide()
        video = ET.fromstring(self.dom.XMLDesc(0)).find('./devices/video/model')
        self.qxl = video is not None and video.get('type') == 'qxl'
        self.session = SpiceClientGLib.Session()
        self.session.set_property('uri', uri)
        self.session.set_property('enable-audio', False)
        gtk_session = SpiceClientGtk.GtkSession.get(self.session)
        gtk_session.set_property('auto-clipboard', True)
        GObject.GObject.connect(self.session, 'channel-new', self.on_channel)
        self.session.connect()

    def on_channel(self, _session, channel):
        if isinstance(channel, SpiceClientGLib.MainChannel):
            self.main = channel
            GObject.GObject.connect(channel, 'channel-event', self.on_main_event)
            GObject.GObject.connect(channel, 'notify::agent-connected', lambda *_: GLib.idle_add(self.on_agent))
        if isinstance(channel, SpiceClientGLib.DisplayChannel) and self.display is None:
            GObject.GObject.connect(channel, 'display-primary-create',
                                    lambda _c, _fmt, w, h, *_rest: setattr(self, 'guest_size', (w, h)))
            self.display_channel_id = channel.get_property('channel-id')
            self.make_display()

    def make_display(self):
        self.display = SpiceClientGtk.Display.new(self.session, self.display_channel_id)
        self.display.set_property('resize-guest', self.resolution is None)
        self.display.set_property('scaling', True)
        self.display.set_property('grab-keyboard', True)
        self.display.set_property('grab-mouse', False)
        self.display.show()
        self.stack.add_named(self.display, 'display')
        self.stack.set_visible_child_name('display')
        self.display.grab_focus()

    def on_scale_factor(self, *_):
        """Moving between monitors with different scaling (laptop 150% -> Dell 100%) changes the
        window's scale factor; the SPICE widget keeps drawing for the old one (half size in the
        corner). Reconnect: a fresh widget on the old connection stays black until the guest
        redraws, while a new connection always gets the full picture."""
        if self.session is None:
            return
        self.close_session()
        self.ph_title.set_text('Connecting…')
        self.stack.set_visible_child_name('placeholder')
        # give the server a moment to drop the old connection before linking the new one
        GLib.timeout_add(600, lambda: (self.session is None and self.open_session(), False)[1])

    def on_main_event(self, channel, event):
        if channel is not self.main:
            return  # a connection we already replaced or closed; its late events don't matter
        if event in (SpiceClientGLib.ChannelEvent.CLOSED, SpiceClientGLib.ChannelEvent.ERROR_CONNECT,
                     SpiceClientGLib.ChannelEvent.ERROR_IO, SpiceClientGLib.ChannelEvent.ERROR_LINK):
            GLib.idle_add(self.lost_connection)

    def lost_connection(self):
        self.close_session()
        try:
            running = self.dom.info()[0] == libvirt.VIR_DOMAIN_RUNNING
        except libvirt.libvirtError:
            running = False
        if running:  # still up: most likely another viewer took over; don't fight it
            self.auto_connect = False
            self.ph_title.set_text('Disconnected')
            self.ph_text.set_text(f'Another viewer (virt-manager or another Lab Manager window) may have taken over '
                                  f'{self.name.replace("HYD-", "")}.\nA VM can only be shown in one window at a time.')
            self.ph_start.hide()
            self.ph_reconnect.show()
            self.stack.set_visible_child_name('placeholder')
        return False

    def reconnect(self):
        self.ph_reconnect.hide()
        self.auto_connect = True
        self.open_session()

    # -- resolution ---------------------------------------------------------
    def agent_ready(self):
        return bool(self.main and self.main.get_property('agent-connected'))

    def update_res_button(self):
        # Only touch the widgets when something changed: re-setting them every tick re-lays out
        # the header, which resizes the display and makes Windows re-apply its mode (blinking).
        label = 'Auto' if self.resolution is None else '{}×{}'.format(*self.resolution)
        if self.display is None:
            why = 'Connect to the VM first'
        elif not self.qxl:
            why = ("This VM is still on the old graphics card, which can't change resolution. "
                   'Shut it down fully and start it again (a restart inside Windows isn\'t enough).')
        elif not self.agent_ready():
            why = 'Needs the guest tools (SPICE agent) running in this VM: run D:\\virtio-win-guest-tools.exe'
        else:
            why = None
        if (label, why) == getattr(self, '_res_shown', None):
            return
        self._res_shown = (label, why)
        self.res_btn.set_label(label)
        self.res_btn.set_sensitive(why is None)
        self.res_btn.set_tooltip_text(why or 'Display resolution')

    def on_agent(self):
        self.update_res_button()
        if self.agent_ready():
            self.apply_resolution()
        return False

    def resolution_menu(self, widget):
        items = [('Auto', lambda: self.set_resolution(None), True, 'Follows the window size', self.resolution is None), None]
        for w, h in RESOLUTIONS:
            items.append((f'{w}×{h}', lambda r=[w, h]: self.set_resolution(r), True, None, self.resolution == [w, h]))
        popover_menu(widget, items).popup()

    def set_resolution(self, res):
        self.resolution = res
        data = load_settings()
        per_vm = data.setdefault('resolution', {})
        if res is None:
            per_vm.pop(self.name, None)
        else:
            per_vm[self.name] = res
        save_settings(data)
        self.update_res_button()
        self.apply_resolution()

    def apply_resolution(self):
        """Auto: let the display widget size the guest to the window. Fixed: ask the guest's
        SPICE agent for that mode and size the window to fit it, like Hyper-V's console."""
        if not (self.display and self.qxl and self.agent_ready()):
            return
        if self.resolution is None:
            self.display.set_property('resize-guest', True)
            alloc = self.display.get_allocation()
            w, h = alloc.width, alloc.height
        else:
            self.display.set_property('resize-guest', False)
            w, h = self.resolution
            self.fit_window(w, h)
            GLib.timeout_add_seconds(6, self.check_mode, (w, h))
        self.main.update_display_enabled(0, True, False)
        self.main.update_display(0, 0, 0, w, h, False)
        self.main.send_monitor_config()

    def fit_window(self, w, h):
        """Resize the window so the VM shows at w x h, or as large as fits the screen (scaled)."""
        if self.is_fullscreen:
            return  # full screen stays full screen; the picture is scaled
        gdk_win = self.get_window()
        if gdk_win is None:
            return
        area = Gdk.Display.get_default().get_monitor_at_window(gdk_win).get_workarea()
        header = self.get_titlebar().get_allocated_height()
        scale = min(1.0, (area.width - 32) / w, (area.height - header - 48) / h)
        self.unmaximize()
        self.resize(int(w * scale), int(h * scale) + header)

    def check_mode(self, wanted):
        if self.display and self.resolution == list(wanted) and self.guest_size != wanted:
            got = '{}×{}'.format(*self.guest_size) if self.guest_size else 'its old size'
            self.toast.show(f"Windows stayed at {got} instead of {wanted[0]}×{wanted[1]}. It may not offer that "
                            'mode yet; try another size, or sign in and try again.', 8)
        return False

    def close_session(self):
        self.main = None
        self.guest_size = None
        if self.display:
            self.stack.remove(self.display)
            self.display.destroy()
            self.display = None
        if self.session:
            self.session.disconnect()
            self.session = None
        return False

    # -- actions ----------------------------------------------------------
    def send_cad(self):
        if self.display:
            self.display.send_keys([Gdk.KEY_Control_L, Gdk.KEY_Alt_L, Gdk.KEY_Delete],
                                   SpiceClientGtk.DisplayKeyEvent.CLICK)

    def start_vm(self):
        self.app.start_vm(self.name, toaster=self.toast)

    def power_menu(self, widget):
        try:
            state = self.dom.info()[0]
        except libvirt.libvirtError:
            return
        popover_menu(widget, self.app.power_items(self.name, self, toaster=self.toast, state=state)).popup()

    def toggle_fullscreen(self):
        if self.is_fullscreen:
            self.unfullscreen()
        else:
            self.fullscreen()

    def on_window_state(self, _w, event):
        self.is_fullscreen = bool(event.new_window_state & Gdk.WindowState.FULLSCREEN)
        set_icon(self.fs_btn, 'view-restore-symbolic' if self.is_fullscreen else 'view-fullscreen-symbolic')
        if not self.is_fullscreen:
            self.fs_rev.set_reveal_child(False)

    def check_top_edge(self):
        win = self.get_window()
        seat = Gdk.Display.get_default().get_default_seat()
        if win is None or seat is None:
            return
        _w, _x, y, _m = win.get_device_position(seat.get_pointer())
        if y <= 3:
            self.fs_rev.set_reveal_child(True)
        elif y > 60:
            self.fs_rev.set_reveal_child(False)

    def on_destroy(self, *_):
        GLib.source_remove(self._poll)
        self.close_session()
        self.app.consoles.pop(self.name, None)
        self.app.maybe_quit()


class ConsoleGrid(Gtk.ScrolledWindow):
    """Live previews of every VM. Pictures come from libvirt screenshots (not SPICE), so they
    never take a VM's single viewer slot; click a tile to open its console."""
    TILE_W, TILE_H = 352, 220
    INTERVAL = 3  # seconds between refreshes, only while this view is showing

    def __init__(self, app, names):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.app = app
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                max_children_per_line=4, min_children_per_line=1,
                                column_spacing=10, row_spacing=10, margin=16, valign=Gtk.Align.START)
        self.add(self.flow)
        self.tiles = {}
        for n in names:
            self.tiles[n] = self._tile(n)
            self.flow.add(self.tiles[n]['root'])
        self.active = False
        self.woken = set()  # VMs woken automatically since the view was last opened
        self._stop = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    def _tile(self, name):
        kind = LAB.get(name, ('client',))[0]
        ev = Gtk.EventBox()
        ev.set_tooltip_text(f'Open the {name.replace("HYD-", "")} console')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.get_style_context().add_class('tile')
        screen = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        screen.set_size_request(self.TILE_W, self.TILE_H)
        screen.get_style_context().add_class('tile-screen')
        image = Gtk.Image()
        screen.add_named(image, 'live')
        asleep = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        asleep_label = Gtk.Label(label='Screen asleep')
        asleep_label.get_style_context().add_class('tile-off')
        asleep.pack_start(asleep_label, False, False, 0)
        wake = text_button('Wake', 'subtle', lambda: self.wake(name))
        wake.set_tooltip_text('Press Shift in the VM to turn its screen back on')
        wake.set_halign(Gtk.Align.CENTER)
        asleep.pack_start(wake, False, False, 0)
        screen.add_named(asleep, 'asleep')
        off = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        off_icon = Gtk.Image.new_from_pixbuf(tile_pixbuf(kind, False, size=96))
        off.pack_start(off_icon, False, False, 0)
        off_label = Gtk.Label()
        off_label.get_style_context().add_class('tile-off')
        off.pack_start(off_label, False, False, 0)
        start = text_button('Start', 'accent', lambda: self.app.start_vm(name))
        start.set_halign(Gtk.Align.CENTER)
        off.pack_start(start, False, False, 0)
        screen.add_named(off, 'off')
        bottom = Gtk.Box(spacing=8)
        label = Gtk.Label(label=name.replace('HYD-', ''), xalign=0)
        label.get_style_context().add_class('tile-name')
        pill = Gtk.Label()
        pill.get_style_context().add_class('pill')
        pill.set_valign(Gtk.Align.CENTER)
        bottom.pack_start(label, False, False, 0)
        bottom.pack_start(pill, False, False, 0)
        bottom.pack_end(text_button('Connect', 'subtle', lambda: self.app.open_console(name)), False, False, 0)
        box.pack_start(screen, False, False, 0)
        box.pack_start(bottom, False, False, 0)
        ev.add(box)
        ev.connect('button-release-event', lambda _w, e: e.button == 1 and self.app.open_console(name))
        return {'root': ev, 'screen': screen, 'image': image, 'off_label': off_label, 'start': start,
                'off_icon': off_icon, 'kind': kind,
                'pill': pill, 'state': None}

    def refresh_states(self):
        """Pills and off placeholders, from the list view's cached states (no extra libvirt calls)."""
        for name, t in self.tiles.items():
            row = self.app.rows.get(name)
            key = row.key if row else None
            if key == t.get('key'):
                continue
            t['key'] = key
            state = row.state if row else None
            t['state'] = state
            text, cls = STATE_TEXT.get(key, ('Unknown', 'off'))
            ctx = t['pill'].get_style_context()
            for c in ('running', 'off', 'busy', 'paused', 'error', 'saved'):
                ctx.remove_class(c)
            ctx.add_class(cls)
            t['pill'].set_text(text)
            if state == libvirt.VIR_DOMAIN_RUNNING:
                if t['image'].get_pixbuf() is not None:
                    t['screen'].set_visible_child_name('live')
            else:
                t['off_label'].set_text('Frozen · resumes where it left off' if key == 'saved' else text)
                t['off_icon'].set_from_pixbuf(tile_pixbuf(t['kind'], False, frozen=key == 'saved', size=96))
                t['start'].set_label('Resume' if key == 'saved' else 'Start')
                t['start'].set_visible(state == libvirt.VIR_DOMAIN_SHUTOFF)
                t['screen'].set_visible_child_name('off')
                t['image'].clear()

    def _worker(self):
        conn = None
        while not self._stop.is_set():
            if self.active:
                try:
                    conn = conn or libvirt.open(URI)
                    for name in list(self.tiles):
                        dom = conn.lookupByName(name)
                        if dom.info()[0] != libvirt.VIR_DOMAIN_RUNNING:
                            continue
                        data = self._screenshot(conn, dom)
                        GLib.idle_add(self._show, name, data)
                except libvirt.libvirtError:
                    conn = None  # reconnect next round
            self._stop.wait(self.INTERVAL)

    @staticmethod
    def _screenshot(conn, dom):
        stream = conn.newStream(0)
        dom.screenshot(stream, 0, 0)
        buf = bytearray()
        stream.recvAll(lambda _st, data, b: b.extend(data) or 0, buf)
        stream.finish()
        return bytes(buf)

    def _show(self, name, data):
        t = self.tiles.get(name)
        if t is None or t['state'] != libvirt.VIR_DOMAIN_RUNNING:
            return False
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pix = loader.get_pixbuf()
        except GLib.Error:
            return False
        scale = min(self.TILE_W / pix.get_width(), self.TILE_H / pix.get_height())
        small = pix.scale_simple(max(1, int(pix.get_width() * scale)), max(1, int(pix.get_height() * scale)),
                                 GdkPixbuf.InterpType.BILINEAR)
        t['image'].set_from_pixbuf(small)
        # Windows turns the display off when idle: an all-black frame. Say so instead of a black box.
        pixels = small.get_pixels()
        blank = max(pixels[::97]) < 12
        if blank and name not in self.woken:
            # Wake it once per opening of the view; the guest-integration GPO keeps it awake after that.
            self.woken.add(name)
            self.wake(name, quiet=True)
        t['screen'].set_visible_child_name('asleep' if blank else 'live')
        return False

    def wake(self, name, quiet=False):
        dom = self.app.conn.lookupByName(name)
        # Left Shift (Linux keycode 42): harmless, and enough to wake the guest's display
        press = lambda: dom.sendKey(libvirt.VIR_KEYCODE_SET_LINUX, 0, [42], 1, 0)
        if quiet:
            run_bg(press)
        else:
            self.app.act(name, 'Waking', press)

    def stop(self):
        self._stop.set()


def fmt_bytes(n, digits=1):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.{digits}f} {unit}'
        n /= 1024


def fmt_rate(n):
    return fmt_bytes(max(0, n)) + '/s'


def read_host():
    """Host CPU counters, memory and swap from /proc."""
    with open('/proc/stat') as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    mem = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, val = line.split(':')
            mem[k] = int(val.split()[0]) * 1024
    return {'busy': sum(v) - v[3] - v[4], 'total': sum(v), 'mem': mem}


def cpu_model():
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.startswith('model name'):
                    return re.sub(r'\((R|TM)\)|\bCPU\b|@.*', '', line.split(':', 1)[1]).split()
    except OSError:
        pass
    return []


class Meter(Gtk.DrawingArea):
    """A rounded bar split into coloured segments: [(fraction, (r, g, b, a)), ...]."""
    def __init__(self, height=10):
        super().__init__()
        self.set_size_request(-1, height)
        self.segments = []
        self.connect('draw', self._draw)

    def set(self, segments):
        if segments != self.segments:
            self.segments = segments
            self.queue_draw()

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        r = h / 2
        cr.new_sub_path()
        cr.arc(r, r, r, 1.5708, 4.7124)
        cr.arc(w - r, r, r, 4.7124, 1.5708)
        cr.close_path()
        cr.save()
        cr.clip_preserve()
        cr.set_source_rgba(1, 1, 1, 0.07)
        cr.fill()
        x = 0.0
        for frac, rgba in self.segments:
            seg = max(0.0, min(1.0, frac)) * w
            cr.rectangle(x, 0, seg, h)
            cr.set_source_rgba(*rgba)
            cr.fill()
            x += seg
        cr.restore()
        return False


LAB_RGBA = (*ACCENT, 0.95)
OTHER_RGBA = (0.62, 0.62, 0.62, 0.55)
OVERHEAD_RGBA = (0.99, 0.88, 0.0, 0.85)


class ResourceView(Gtk.ScrolledWindow):
    """What the lab costs the host: host CPU / memory / storage with the lab's share, the impact in
    plain words, and a per-VM table. Numbers are host-side (what each VM's QEMU process really uses),
    sampled from libvirt and /proc on the main window's 2 s tick."""
    COLS = ('VM', 'CPU', 'Memory on host', 'Disk I/O', 'Network', 'Disk on host')
    RESERVE = 4 << 30  # RAM to leave for Linux and your apps before calling it "room for another VM"

    def __init__(self, app, names):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.app, self.names = app, names
        self.active = False
        self.prev = None  # (monotonic s, host sample, {vm: stats})
        self.last = None  # the latest render() arguments, shown straight away when the view opens
        self.ncpu = os.cpu_count() or 1
        self.model = ' '.join(cpu_model())
        self.paths = {}
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=20, margin_end=20, margin_bottom=20, margin_top=4)
        self.add(body)

        title = Gtk.Label(label='Host', xalign=0)
        title.get_style_context().add_class('section')
        body.pack_start(title, False, False, 0)
        cards = Gtk.Box(spacing=8, homogeneous=True)
        body.pack_start(cards, False, False, 0)
        self.cpu_card = self._card(cards, 'Processor', spark=True)
        self.mem_card = self._card(cards, 'Memory')
        self.disk_card = self._card(cards, 'Storage')

        title = Gtk.Label(label='Impact of the lab', xalign=0)
        title.get_style_context().add_class('section')
        body.pack_start(title, False, False, 0)
        impact = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        impact.get_style_context().add_class('card')
        body.pack_start(impact, False, False, 0)
        self.impact = []
        for _ in range(5):
            lbl = Gtk.Label(xalign=0, wrap=True, use_markup=True)
            lbl.get_style_context().add_class('impact')
            impact.pack_start(lbl, False, False, 0)
            self.impact.append(lbl)

        title = Gtk.Label(label='Virtual machines', xalign=0)
        title.get_style_context().add_class('section')
        body.pack_start(title, False, False, 0)
        table = Gtk.Grid(column_spacing=18, row_spacing=0)
        table.get_style_context().add_class('card')
        body.pack_start(table, False, False, 0)
        for c, text in enumerate(self.COLS):
            h = Gtk.Label(label=text, xalign=0)
            h.get_style_context().add_class('res-head')
            table.attach(h, c, 0, 1, 1)
        self.cells = {}
        for r, name in enumerate(names + ['Total'], start=1):
            cells = []
            for c in range(len(self.COLS)):
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=c > 0)
                box.get_style_context().add_class('res-cell')
                main = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
                main.get_style_context().add_class('res-total' if name == 'Total' else 'res-main')
                sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
                sub.get_style_context().add_class('res-sub')
                box.pack_start(main, False, False, 0)
                box.pack_start(sub, False, False, 0)
                table.attach(box, c, r, 1, 1)
                cells.append((main, sub))
            cells[0][0].set_text(name.replace('HYD-', ''))
            self.cells[name] = cells
        note = Gtk.Label(xalign=0, wrap=True, label=(
            'Memory is what each VM really holds in host RAM (its QEMU process), including QEMU’s own '
            'overhead. What Windows uses inside the VM isn’t visible without a balloon device. CPU % is '
            'of the VM’s own vCPUs; “host” is its share of all host threads.'))
        note.get_style_context().add_class('res-sub')
        note.set_margin_top(8)
        body.pack_start(note, False, False, 0)

    @staticmethod
    def _card(parent, title, spark=False):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.get_style_context().add_class('card')
        t = Gtk.Label(label=title, xalign=0)
        t.get_style_context().add_class('res-sub')
        big = Gtk.Label(xalign=0)
        big.get_style_context().add_class('res-big')
        meter = Meter()
        legend = Gtk.Label(xalign=0, wrap=True, use_markup=True)
        legend.get_style_context().add_class('res-sub')
        for w in (t, big, meter, legend):
            box.pack_start(w, False, False, 0)
        card = {'big': big, 'meter': meter, 'legend': legend}
        if spark:
            card['spark'] = Sparkline()
            card['spark'].set_size_request(-1, 30)
            box.pack_start(card['spark'], False, False, 0)
        parent.pack_start(box, True, True, 0)
        return card

    def _disk_used(self, name, dom):
        """Space the VM's disk image really takes on the host (sparse qcow2: allocated blocks)."""
        try:
            if name not in self.paths:
                self.paths[name] = disk_path(dom)
            return os.stat(self.paths[name]).st_blocks * 512
        except (OSError, RuntimeError, libvirt.libvirtError):
            return 0

    def sample(self):
        """Take a sample (every tick, so rates are ready when the view opens); redraw if showing."""
        flags = (libvirt.VIR_DOMAIN_STATS_STATE | libvirt.VIR_DOMAIN_STATS_CPU_TOTAL | libvirt.VIR_DOMAIN_STATS_BALLOON
                 | libvirt.VIR_DOMAIN_STATS_INTERFACE | libvirt.VIR_DOMAIN_STATS_BLOCK | libvirt.VIR_DOMAIN_STATS_VCPU)
        try:
            stats = {d.name(): (d, s) for d, s in self.app.conn.getAllDomainStats(flags, 0)}
            host = read_host()
        except (libvirt.libvirtError, OSError):
            return
        now = time.monotonic()
        prev, self.prev = self.prev, (now, host, stats)
        if prev:
            self.last = (now - prev[0], prev[1], prev[2], host, stats)
            if self.active:
                self.render(*self.last)

    def render(self, dt, h0, s0, h1, s1):
        GB = 1 << 30
        rows, tot = {}, dict(cpu=0.0, vcpu_t=0.0, vcpus=0, rss=0, assigned=0, rd=0.0, wr=0.0, rx=0.0, tx=0.0, disk=0, running=0)
        for name in self.names:
            if name not in s1:
                continue
            dom, s = s1[name]
            old = s0.get(name, (None, {}))[1]
            running = s.get('state.state') == libvirt.VIR_DOMAIN_RUNNING
            row = {'running': running, 'disk': self._disk_used(name, dom), 'key': getattr(self.app.rows.get(name), 'key', None)}
            row['vcpus'] = s.get('vcpu.current') or dom.info()[3]
            row['assigned'] = s.get('balloon.current', dom.info()[2]) * 1024
            if running and old.get('state.state') == libvirt.VIR_DOMAIN_RUNNING:
                d = lambda k: max(0, s.get(k, 0) - old.get(k, 0)) / dt
                vt = sum(d(f'vcpu.{i}.time') for i in range(row['vcpus']))
                row.update(cpu=d('cpu.time') / 1e9, vcpu_t=vt / 1e9, rss=s.get('balloon.rss', 0) * 1024,
                           rd=sum(d(f'block.{i}.rd.bytes') for i in range(s.get('block.count', 0))),
                           wr=sum(d(f'block.{i}.wr.bytes') for i in range(s.get('block.count', 0))),
                           rx=sum(d(f'net.{i}.rx.bytes') for i in range(s.get('net.count', 0))),
                           tx=sum(d(f'net.{i}.tx.bytes') for i in range(s.get('net.count', 0))))
                for k in ('cpu', 'vcpu_t', 'rss', 'rd', 'wr', 'rx', 'tx'):
                    tot[k] += row[k]
                tot['vcpus'] += row['vcpus']
                tot['assigned'] += row['assigned']
                tot['running'] += 1
            tot['disk'] += row['disk']
            rows[name] = row
            self._fill_row(name, row)

        # Totals row
        cells = self.cells['Total']
        cells[0][1].set_text(f'{tot["running"]} running')
        cells[1][0].set_text(f'{tot["cpu"] / self.ncpu * 100:.1f}% of host')
        cells[1][1].set_text(f'{tot["vcpus"]} vCPU · {tot["cpu"]:.2f} threads busy')
        cells[2][0].set_text(fmt_bytes(tot['rss']))
        cells[2][1].set_text(f'{fmt_bytes(tot["assigned"], 0)} assigned · +{fmt_bytes(max(0, tot["rss"] - tot["assigned"]), 0)} QEMU')
        cells[3][0].set_text(f'R {fmt_rate(tot["rd"])}')
        cells[3][1].set_text(f'W {fmt_rate(tot["wr"])}')
        cells[4][0].set_text(f'↓ {fmt_rate(tot["rx"])}')
        cells[4][1].set_text(f'↑ {fmt_rate(tot["tx"])}')
        cells[5][0].set_text(fmt_bytes(tot['disk']))
        cells[5][1].set_text('all lab disks')

        # Host cards
        span = max(1, h1['total'] - h0['total'])
        host_pct = (h1['busy'] - h0['busy']) / span * 100
        lab_pct = min(host_pct, tot['cpu'] / self.ncpu * 100)
        c = self.cpu_card
        c['big'].set_text(f'{host_pct:.0f}%')
        c['meter'].set([(lab_pct / 100, LAB_RGBA), ((host_pct - lab_pct) / 100, OTHER_RGBA)])
        c['legend'].set_markup(f'<span foreground="#60cdff">■</span> Lab {lab_pct:.1f}%   '
                               f'<span foreground="#9e9e9e">■</span> Other {host_pct - lab_pct:.1f}%\n'
                               f'{self.ncpu} threads · {GLib.markup_escape_text(self.model)}')
        c['spark'].push(host_pct)

        mem = h1['mem']
        total, avail = mem['MemTotal'], mem['MemAvailable']
        other = max(0, total - avail - tot['rss'])
        c = self.mem_card
        c['big'].set_text(f'{(total - avail) / GB:.1f} of {total / GB:.1f} GB')
        c['meter'].set([(tot['rss'] / total, LAB_RGBA), (other / total, OTHER_RGBA)])
        c['legend'].set_markup(f'<span foreground="#60cdff">■</span> Lab {fmt_bytes(tot["rss"])}   '
                               f'<span foreground="#9e9e9e">■</span> Other {fmt_bytes(other)}\n'
                               f'{fmt_bytes(avail)} available · swap {fmt_bytes(mem.get("SwapTotal", 0), 0) if mem.get("SwapTotal") else "none"}')

        fs = os.statvfs(os.path.dirname(next(iter(self.paths.values()), '/')) or '/')
        fs_total, fs_free = fs.f_blocks * fs.f_frsize, fs.f_bavail * fs.f_frsize
        fs_other = max(0, fs_total - fs_free - tot['disk'])
        c = self.disk_card
        c['big'].set_text(f'{fmt_bytes(fs_free)} free')
        c['meter'].set([(tot['disk'] / fs_total, LAB_RGBA), (fs_other / fs_total, OTHER_RGBA)])
        c['legend'].set_markup(f'<span foreground="#60cdff">■</span> Lab disks {fmt_bytes(tot["disk"])}   '
                               f'<span foreground="#9e9e9e">■</span> Other {fmt_bytes(fs_other)}\n'
                               f'of {fmt_bytes(fs_total)} on the disk holding the VM images')

        self._impact(tot, host_pct, lab_pct, total, avail, mem, fs_free)

    def _fill_row(self, name, row):
        cells = self.cells[name]
        text, _cls = STATE_TEXT.get(row['key'], ('Off', 'off'))
        cells[0][1].set_text(text)
        if not row['running'] or 'cpu' not in row:
            cells[1][0].set_text('—')
            cells[1][1].set_text(f'{row["vcpus"]} vCPU')
            cells[2][0].set_text('—')
            cells[2][1].set_text(f'{fmt_bytes(row["assigned"], 0)} assigned')
            for c in (3, 4):
                cells[c][0].set_text('—')
                cells[c][1].set_text('')
        else:
            vm_pct = min(100.0, row['cpu'] / row['vcpus'] * 100)
            cells[1][0].set_text(f'{vm_pct:.0f}%')
            cells[1][1].set_text(f'{row["vcpus"]} vCPU · {row["cpu"] / self.ncpu * 100:.1f}% host')
            cells[2][0].set_text(fmt_bytes(row['rss']))
            cells[2][1].set_text(f'{fmt_bytes(row["assigned"], 0)} assigned · +{fmt_bytes(max(0, row["rss"] - row["assigned"]), 0)} QEMU')
            cells[3][0].set_text(f'R {fmt_rate(row["rd"])}')
            cells[3][1].set_text(f'W {fmt_rate(row["wr"])}')
            cells[4][0].set_text(f'↓ {fmt_rate(row["rx"])}')
            cells[4][1].set_text(f'↑ {fmt_rate(row["tx"])}')
        cells[5][0].set_text(fmt_bytes(row['disk']) if row['disk'] else '—')
        cells[5][1].set_text('frozen: + saved RAM' if row['key'] == 'saved' else '')

    def _impact(self, tot, host_pct, lab_pct, total, avail, mem, fs_free):
        GB = 1 << 30
        b = lambda s: f'<b>{s}</b>'
        lines = []
        if tot['running']:
            emu = max(0.0, tot['cpu'] - tot['vcpu_t'])
            lines.append(f'CPU: the lab uses {b(f"{lab_pct:.1f}%")} of the host ({tot["cpu"]:.2f} of {self.ncpu} threads), '
                         f'of which {emu:.2f} threads is virtualization overhead (QEMU device emulation and I/O).')
            lines.append(f'Memory: the {tot["running"]} running VMs hold {b(fmt_bytes(tot["rss"]))} '
                         f'({tot["rss"] / total * 100:.0f}% of host RAM), {fmt_bytes(max(0, tot["rss"] - tot["assigned"]), 0)} '
                         f'of it QEMU overhead on top of what the VMs were given. {b(fmt_bytes(avail))} is left for Linux and your apps.')
            ratio = tot['vcpus'] / self.ncpu
            lines.append(f'Overcommit: {tot["vcpus"]} vCPUs on {self.ncpu} host threads ({ratio:.2f}:1) and '
                         f'{fmt_bytes(tot["assigned"], 0)} of RAM assigned out of {fmt_bytes(total, 0)}: '
                         + ('no overcommit, each vCPU can get a whole thread.' if ratio <= 1 else 'vCPUs share threads under load.'))
        else:
            lines.append('No VMs are running, so the lab isn’t using any CPU or memory right now.')
        room = int(max(0, avail - self.RESERVE) // (4.3 * GB))  # a 4 GB client + QEMU overhead
        lines.append(f'Headroom: room to start {b(str(room))} more 4 GB client{"s" if room != 1 else ""} while keeping '
                     f'{fmt_bytes(self.RESERVE, 0)} free for the host.')
        if not mem.get('SwapTotal'):
            lines.append('<span foreground="#fce100">No swap on this host:</span> if RAM runs out, Linux stops the '
                         'biggest process (most likely a VM, such as CM1) instead of slowing down. Freeze lab frees RAM safely.')
        elif fs_free < 50 * GB:
            lines.append('<span foreground="#fce100">Low disk space:</span> VM disks and frozen memory images grow into it.')
        for lbl, text in zip(self.impact, lines + [''] * len(self.impact)):
            if lbl.get_label() != text:
                lbl.set_markup(text)
            lbl.set_visible(bool(text))


class LabManager:
    def __init__(self, gapp=None):
        self.gapp = gapp  # Gtk.Application when run normally; None in tests driven by Gtk.main()
        self.conn = connect()
        self.consoles = {}
        self.window = None
        self.rows = {}
        self.lab_run = None  # while Start lab runs: {'cancel': Event, 'touched': set, 'status': str}

    # -- main window ------------------------------------------------------
    def build_main(self):
        win = Gtk.Window(title='Lab Manager')
        win.get_style_context().add_class('lab')
        win.set_default_size(940, 780)
        win.set_icon_name('computer')
        hb = Gtk.HeaderBar(show_close_button=True, title='Lab Manager')
        self.header = hb
        self.start_lab_btn = text_button('Start lab', 'accent', self.start_lab)
        self.start_lab_btn.set_tooltip_text('Start DC1, GW1, CM1, then CLIENT1–4, each after the previous is up')
        hb.pack_start(self.start_lab_btn)
        self.freeze_btn = text_button('Freeze lab', 'subtle', self.freeze_lab)
        self.freeze_btn.set_image(Gtk.Image.new_from_icon_name('weather-snow-symbolic', Gtk.IconSize.BUTTON))
        self.freeze_btn.set_always_show_image(True)
        self.freeze_btn.set_tooltip_text('Save every running VM to disk and free its RAM; Resume lab brings back exactly these')
        hb.pack_start(self.freeze_btn)
        self.view_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.view_stack)
        switcher.set_valign(Gtk.Align.CENTER)
        hb.pack_start(switcher)
        hb.pack_end(icon_button('preferences-system-symbolic', 'Open virt-manager (advanced settings)',
                                lambda: subprocess.Popen(['virt-manager', '--connect', URI])))
        win.set_titlebar(hb)

        overlay = Gtk.Overlay()
        self.toast = Toaster(overlay)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=20, margin_end=20, margin_bottom=20, margin_top=4)
        doms = {d.name(): d for d in self.conn.listAllDomains()}
        names = [n for n in LAB if n in doms] + sorted(n for n in doms if n not in LAB)
        for section, kind in (('Servers', 'server'), ('Clients', 'client')):
            members = [n for n in names if LAB.get(n, ('client',))[0] == kind]
            if not members:
                continue
            title = Gtk.Label(label=section, xalign=0)
            title.get_style_context().add_class('section')
            body.pack_start(title, False, False, 0)
            for n in members:
                row = VmRow(self, doms[n])
                self.rows[n] = row
                body.pack_start(row, False, False, 0)
        sw.add(body)
        self.grid = ConsoleGrid(self, names)
        self.view_stack.add_titled(sw, 'list', 'List')
        self.view_stack.add_titled(self.grid, 'consoles', 'Consoles')
        self.resources = ResourceView(self, names)
        self.view_stack.add_titled(self.resources, 'resources', 'Resources')
        self.view_stack.connect('notify::visible-child-name', self.on_view_changed)
        overlay.add(self.view_stack)
        win.add(overlay)
        win.connect('destroy', lambda *_: (self.grid.stop(), setattr(self, 'window', None), self.maybe_quit()))
        self.window = win
        if self.gapp:
            self.gapp.add_window(win)
        GLib.timeout_add(2000, self.tick)
        self.tick()
        win.show_all()
        view = load_settings().get('view')
        if view == 'consoles':
            win.set_default_size(1180, 820)
            win.resize(1180, 820)
        if view in ('consoles', 'resources'):
            self.view_stack.set_visible_child_name(view)

    def on_view_changed(self, *_):
        view = self.view_stack.get_visible_child_name()
        self.grid.active = view == 'consoles'
        self.resources.active = view == 'resources'
        if self.resources.active and self.resources.last:
            self.resources.render(*self.resources.last)
        if self.grid.active:
            self.grid.woken.clear()
            self.grid.refresh_states()
            w, h = self.window.get_size()
            if w < 1100:  # room for three previews side by side
                self.window.resize(1180, max(h, 820))
        data = load_settings()
        data['view'] = view
        save_settings(data)

    def tick(self):
        if self.window is None:
            return False
        for row in self.rows.values():
            row.refresh()
        if self.grid.active:
            self.grid.refresh_states()
        self.resources.sample()
        running = sum(1 for r in self.rows.values() if r.running)
        saved = sum(1 for r in self.rows.values() if r.key == 'saved')
        run = self.lab_run
        subtitle = run['status'] if run else (f'corp.contoso.com · {running} of {len(self.rows)} running'
                                              + (f' · {saved} frozen' if saved else ''))
        if self.header.get_subtitle() != subtitle:
            self.header.set_subtitle(subtitle)
        if not run:
            label = 'Resume lab' if saved else 'Start lab'
            if self.start_lab_btn.get_label() != label:
                self.start_lab_btn.set_label(label)
                self.start_lab_btn.set_tooltip_text(
                    'Resume the frozen VMs (DC1, GW1, CM1 first), right where they left off' if saved else
                    'Start DC1, GW1, CM1, then CLIENT1–4, each after the previous is up')
        freeze_ok = running > 0 and run is None
        if self.freeze_btn.get_sensitive() != freeze_ok:
            self.freeze_btn.set_sensitive(freeze_ok)
        return True

    # -- shared -----------------------------------------------------------
    def act(self, name, verb, fn, toaster=None):
        if self.lab_run:
            self.lab_run['touched'].add(name)
        toaster = toaster or self.toast
        toaster.show(f'{verb} {name.replace("HYD-", "")}…', 3)

        def done(_res, err):
            if err:
                toaster.show(f'{name.replace("HYD-", "")}: {err}', 6)
        run_bg(fn, done)

    def power_items(self, name, parent, toaster=None, state=None):
        """The power menu, VMware style: safe actions go through Windows, hard ones don't."""
        dom = self.conn.lookupByName(name)
        if state is None:
            try:
                state = dom.info()[0]
            except libvirt.libvirtError:
                state = libvirt.VIR_DOMAIN_NOSTATE
        off = state == libvirt.VIR_DOMAIN_SHUTOFF
        running = state == libvirt.VIR_DOMAIN_RUNNING
        if off and dom.hasManagedSaveImage(0):
            return [('Resume', lambda: self.start_vm(name, toaster), True, 'Continues exactly where it was saved'),
                    ('Discard saved state', lambda: self.discard_save(name, parent, toaster), True,
                     'Next start boots fresh instead'),]
        if off:
            return [('Start', lambda: self.start_vm(name, toaster), True, 'Power on the VM')]
        return [
            ('header', 'Safe — Windows shuts down cleanly'),
            ('Shut down', lambda: self.shutdown(name, toaster), running, 'Saves work, closes apps, then powers off'),
            ('Restart', lambda: self.restart(name, toaster), running, 'Windows restarts itself'),
            None,
            ('header', 'Hard — like the physical buttons'),
            ('Power off', lambda: self.hard(name, parent, toaster, 'off'), True, 'Cuts power instantly; unsaved work is lost'),
            ('Reset', lambda: self.hard(name, parent, toaster, 'reset'), running, 'Instant reboot without shutting down'),
            None,
            ('header', 'Suspend'),
            ('Save state', lambda: self.save_vm(name, toaster), running,
             'Saves memory to disk and frees its RAM; Resume continues right here'),
        ]

    def start_vm(self, name, toaster=None):
        """Start a VM; if it has a saved state this resumes it, then re-syncs its clock."""
        dom = self.conn.lookupByName(name)
        saved = dom.hasManagedSaveImage(0)

        def work():
            dom.create()
            if saved:
                self.sync_clock(dom)
        if self.lab_run:
            self.lab_run['touched'].add(name)
        (toaster or self.toast).show(f'{"Resuming" if saved else "Starting"} {name.replace("HYD-", "")}…', 3)
        run_bg(work, lambda _r, err: err and (toaster or self.toast).show(f'{name.replace("HYD-", "")}: {err}', 6))

    @staticmethod
    def sync_clock(dom):
        """A resumed VM's clock is behind by however long it was saved, which upsets a domain
        (Kerberos). Set it to the host's UTC time through the guest agent when the VM has one.
        (Not VIR_DOMAIN_TIME_SYNC: that copies the VM's RTC, which runs on host local time and put
        a client 2 hours off.)"""
        for _ in range(10):
            try:
                now = time.time()
                dom.setTime({'seconds': int(now), 'nseconds': int((now % 1) * 1e9)}, 0)
                return True
            except libvirt.libvirtError:
                time.sleep(2)  # agent not answering yet
        return False

    def save_vm(self, name, toaster=None):
        dom = self.conn.lookupByName(name)
        self.act(name, 'Saving state of', lambda: dom.managedSave(0), toaster=toaster)

    def discard_save(self, name, parent, toaster):
        dlg = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.OK_CANCEL, text=f'Discard {name.replace("HYD-", "")}\'s saved state?')
        dlg.format_secondary_text('Its next start boots Windows fresh, like after a power cut: anything '
                                  'unsaved in the frozen session is lost.')
        ok = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        if ok:
            dom = self.conn.lookupByName(name)
            self.act(name, 'Discarding saved state of', lambda: dom.managedSaveRemove(0), toaster=toaster)

    SAFE_FLAGS = libvirt.VIR_DOMAIN_SHUTDOWN_GUEST_AGENT | libvirt.VIR_DOMAIN_SHUTDOWN_ACPI_POWER_BTN

    def shutdown(self, name, toaster=None):
        """Safe shut down: through the guest agent if the VM has one, else the ACPI power button.
        Windows ignores the power button while it's still booting, so check back later."""
        dom = self.conn.lookupByName(name)
        self.act(name, 'Shutting down', lambda: dom.shutdownFlags(self.SAFE_FLAGS), toaster=toaster)
        self._still_running_hint(dom, name, toaster or self.toast, 'shut down')

    def restart(self, name, toaster=None):
        dom = self.conn.lookupByName(name)
        self.act(name, 'Restarting', lambda: dom.reboot(self.SAFE_FLAGS), toaster=toaster)

    def _still_running_hint(self, dom, name, toaster, what):
        def check():
            try:
                if dom.info()[0] == libvirt.VIR_DOMAIN_RUNNING:
                    toaster.show(f'{name.replace("HYD-", "")} is still running. Windows ignores {what} while it\'s '
                                 'starting up or busy; try again, or use Power off.', 8)
            except libvirt.libvirtError:
                pass
            return False
        GLib.timeout_add_seconds(90, check)

    def hard(self, name, parent, toaster, kind):
        short = name.replace('HYD-', '')
        text, verb, fn = {
            'off': (f'Power off {short}?', 'Powering off', lambda d: d.destroy()),
            'reset': (f'Reset {short}?', 'Resetting', lambda d: d.reset(0)),
        }[kind]
        dlg = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.NONE, text=text)
        dlg.format_secondary_text('Like pressing the physical button: Windows doesn\'t get to shut down, and '
                                  'unsaved work in the VM is lost. Use Shut down / Restart when you can.')
        dlg.add_button('Cancel', Gtk.ResponseType.CANCEL)
        go = dlg.add_button('Power off' if kind == 'off' else 'Reset', Gtk.ResponseType.OK)
        go.get_style_context().add_class('destructive-action')
        ok = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        if ok:
            dom = self.conn.lookupByName(name)
            self.act(name, verb, lambda: fn(dom), toaster=toaster)

    def start_lab(self):
        if self.lab_run:
            self.lab_run['cancel'].set()
            self.lab_run['status'] = 'Cancelling Start lab…'
            self.start_lab_btn.set_sensitive(False)
            self.tick()
            return
        doms = {d.name(): d for d in self.conn.listAllDomains()}
        frozen = [n for n in START_ORDER + sorted(set(doms) - set(START_ORDER))
                  if n in doms and doms[n].hasManagedSaveImage(0)]
        if frozen:
            return self.resume_lab(doms, frozen)
        run = self.lab_run = {'cancel': threading.Event(), 'touched': set(), 'status': 'Starting lab…'}
        self.start_lab_btn.set_label('Cancel')
        order = [n for n in START_ORDER if n in doms]

        def wait(seconds):
            return run['cancel'].wait(seconds)  # True when cancelled

        def work():
            for i, n in enumerate(order, 1):
                if run['cancel'].is_set():
                    return 'cancelled'
                d = doms[n]
                if n in run['touched'] or d.info()[0] != libvirt.VIR_DOMAIN_SHUTOFF:
                    continue  # already up, or you started/stopped it yourself during the run
                run['status'] = f'Start lab: starting {n.replace("HYD-", "")} · {i} of {len(order)}'
                d.create()
                if n in ('HYD-DC1', 'HYD-GW1'):  # later VMs need these answering on the network
                    deadline = time.time() + 240
                    while time.time() < deadline:
                        try:
                            self.conn.lookupByName(n).qemuAgentCommand('{"execute":"guest-ping"}', 3, 0)
                            break
                        except libvirt.libvirtError:
                            if wait(4):
                                return 'cancelled'
                elif wait(8):
                    return 'cancelled'
            return 'done'

        def done(res, err):
            self.lab_run = None
            self.start_lab_btn.set_label('Start lab')
            self.start_lab_btn.set_sensitive(True)
            self.toast.show(f'Start lab: {err}' if err else
                            'Start lab cancelled' if res == 'cancelled' else 'Lab started', 5)
            self.tick()
        run_bg(work, done)

    def resume_lab(self, doms, frozen):
        """Bring back exactly the frozen VMs, servers first, and re-sync their clocks."""
        run = self.lab_run = {'cancel': threading.Event(), 'touched': set(), 'status': 'Resuming lab…'}
        self.start_lab_btn.set_label('Cancel')

        def work():
            resumed = []
            for i, n in enumerate(frozen, 1):
                if run['cancel'].is_set():
                    return resumed, 'cancelled'
                if n in run['touched']:
                    continue
                run['status'] = f'Resume lab: {n.replace("HYD-", "")} · {i} of {len(frozen)}'
                doms[n].create()  # restores the saved memory image
                resumed.append(n)
                if run['cancel'].wait(2):
                    return resumed, 'cancelled'
            run['status'] = 'Resume lab: syncing clocks…'
            synced = [n for n in resumed if self.sync_clock(doms[n])]
            return resumed, synced

        def done(res, err):
            self.lab_run = None
            self.start_lab_btn.set_sensitive(True)
            if err:
                self.toast.show(f'Resume lab: {err}', 6)
            else:
                resumed, synced = res
                note = 'cancelled' if synced == 'cancelled' else (
                    f'clocks synced on {len(synced)}' if synced else 'no guest agents to sync clocks')
                self.toast.show(f'Resumed {len(resumed)} VM{"s" if len(resumed) != 1 else ""} · {note}', 6)
            self.tick()
        run_bg(work, done)

    def freeze_lab(self):
        running = [n for n in reversed(START_ORDER) if n in self.rows and self.rows[n].running]
        running += [n for n, r in self.rows.items() if r.running and n not in running]
        if not running:
            return
        ram = sum(self.rows[n].dom.info()[2] for n in running) / 1048576
        dlg = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE,
                                text=f'Freeze {len(running)} running VM{"s" if len(running) != 1 else ""}?')
        dlg.format_secondary_text(
            f'{", ".join(n.replace("HYD-", "") for n in running)}.\n\nEach VM\'s memory is saved to disk (up to '
            f'{ram:g} GB in total) and its RAM and CPU are freed. Resume lab brings back exactly these VMs, right '
            'where they left off, even after the laptop restarts.')
        dlg.add_button('Cancel', Gtk.ResponseType.CANCEL)
        dlg.add_button('Freeze', Gtk.ResponseType.OK).get_style_context().add_class('suggested-action')
        ok = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        if not ok:
            return
        run = self.lab_run = {'cancel': threading.Event(), 'touched': set(), 'status': 'Freezing lab…'}
        self.start_lab_btn.set_label('Cancel')
        doms = {n: self.rows[n].dom for n in running}

        def work():
            done_names = []
            for i, n in enumerate(running, 1):  # clients first, servers last
                if run['cancel'].is_set():
                    break
                if n in run['touched']:
                    continue
                run['status'] = f'Freeze lab: saving {n.replace("HYD-", "")} · {i} of {len(running)}'
                doms[n].managedSave(0)
                done_names.append(n)
            return done_names

        def done(res, err):
            self.lab_run = None
            self.start_lab_btn.set_sensitive(True)
            self.toast.show(f'Freeze lab: {err}' if err else
                            f'Froze {len(res)} VM{"s" if len(res) != 1 else ""}. Resume lab brings them back.', 6)
            self.tick()
        run_bg(work, done)

    def open_console(self, name):
        if name in self.consoles:
            self.consoles[name].present()
            return
        self.consoles[name] = Console(self, self.conn.lookupByName(name))
        if self.gapp:
            self.gapp.add_window(self.consoles[name])

    def maybe_quit(self):
        # With Gtk.Application the app ends by itself when its last window closes.
        if self.gapp is None and self.window is None and not self.consoles:
            Gtk.main_quit()


def main():
    GLib.set_prgname('lab-manager')
    GLib.set_application_name('Lab Manager')
    settings = Gtk.Settings.get_default()
    settings.set_property('gtk-application-prefer-dark-theme', True)
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    # One instance per user: launching again hands the arguments to the running copy, so two
    # consoles never fight over the same VM.
    gapp = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
    state = {}

    def command_line(_gapp, cmdline):
        args = cmdline.get_arguments()[1:]
        if 'app' not in state:
            state['app'] = LabManager(gapp)
        app = state['app']
        if len(args) == 2 and args[0] == '--console':
            app.open_console(args[1])
        elif app.window is None:
            app.build_main()
        else:
            app.window.present()
        return 0

    gapp.connect('command-line', command_line)
    sys.exit(gapp.run(sys.argv))


if __name__ == '__main__':
    main()
