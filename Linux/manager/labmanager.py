#!/usr/bin/env python3
"""Lab Manager: a Windows 11-style window for the hydration lab VMs (libvirt qemu:///system).

Main window, two views:
- List: VM cards with role, status, guest tools state, live CPU graph, Connect, Checkpoints, and
  VMware-style power options: safe Shut down / Restart (through Windows) and hard Power off / Reset
  (like the physical buttons, confirmed first).
- Consoles: live previews of every VM from libvirt screenshots, which don't take the VM's one SPICE
  viewer slot; click one to open its console.
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
.fs-bar { background: rgba(32,32,32,0.92); border-radius: 0 0 8px 8px; padding: 4px 8px; }
"""


TILE_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 40 40">
<rect x="0.5" y="0.5" width="39" height="39" rx="9" fill="{bg}" stroke="{rim}"/>{glyph}</svg>'''
GLYPHS = {
    'server': '''<rect x="11" y="9" width="18" height="9" rx="2" fill="none" stroke="{fg}" stroke-width="1.8"/>
<rect x="11" y="22" width="18" height="9" rx="2" fill="none" stroke="{fg}" stroke-width="1.8"/>
<circle cx="15" cy="13.5" r="1.3" fill="{fg}"/><circle cx="15" cy="26.5" r="1.3" fill="{fg}"/>
<path d="M19 13.5h6M19 26.5h6" stroke="{fg}" stroke-width="1.6" stroke-linecap="round"/>''',
    'client': '''<rect x="9" y="10" width="22" height="15" rx="2.5" fill="none" stroke="{fg}" stroke-width="1.8"/>
<path d="M17 29.5h6M20 25v4.5" stroke="{fg}" stroke-width="1.8" stroke-linecap="round"/>''',
}


def tile_pixbuf(kind, active):
    fg = '#60cdff' if active else '#8f8f8f'
    bg = 'rgba(96,205,255,0.13)' if active else 'rgba(255,255,255,0.05)'
    rim = 'rgba(96,205,255,0.35)' if active else 'rgba(255,255,255,0.08)'
    svg = TILE_SVG.format(bg=bg, rim=rim, glyph=GLYPHS[kind].format(fg=fg))
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
        text, cls = STATE_TEXT.get(state, ('Unknown', 'off'))
        if state != self.state:
            ctx = self.pill.get_style_context()
            for c in ('running', 'off', 'busy', 'paused', 'error'):
                ctx.remove_class(c)
            ctx.add_class(cls)
            self.pill.set_text(text)
            running = state == libvirt.VIR_DOMAIN_RUNNING
            set_icon(self.power, 'system-shutdown-symbolic' if running else 'media-playback-start-symbolic')
            self.power.set_tooltip_text('Power options' if running else 'Start')
            self.icon.set_from_pixbuf(tile_pixbuf(self.kind, running))
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
            self.app.act(self.name, 'Starting', self.dom.create)
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
            text, _ = STATE_TEXT.get(state, ('Off', 'off'))
            self.ph_title.set_text(f'{self.name.replace("HYD-", "")} is {text.lower()}')
            self.ph_text.set_text('Start it to connect.' if state == libvirt.VIR_DOMAIN_SHUTOFF else '')
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
        self.app.act(self.name, 'Starting', self.dom.create, toaster=self.toast)

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
        off.pack_start(Gtk.Image.new_from_pixbuf(tile_pixbuf(kind, False)), False, False, 0)
        off_label = Gtk.Label()
        off_label.get_style_context().add_class('tile-off')
        off.pack_start(off_label, False, False, 0)
        start = text_button('Start', 'accent', lambda: self.app.act(name, 'Starting', self.app.conn.lookupByName(name).create))
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
                'pill': pill, 'state': None}

    def refresh_states(self):
        """Pills and off placeholders, from the list view's cached states (no extra libvirt calls)."""
        for name, t in self.tiles.items():
            row = self.app.rows.get(name)
            state = row.state if row else None
            if state == t['state']:
                continue
            t['state'] = state
            text, cls = STATE_TEXT.get(state, ('Unknown', 'off'))
            ctx = t['pill'].get_style_context()
            for c in ('running', 'off', 'busy', 'paused', 'error'):
                ctx.remove_class(c)
            ctx.add_class(cls)
            t['pill'].set_text(text)
            if state == libvirt.VIR_DOMAIN_RUNNING:
                if t['image'].get_pixbuf() is not None:
                    t['screen'].set_visible_child_name('live')
            else:
                t['off_label'].set_text(f'{text}')
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
        t['screen'].set_visible_child_name('asleep' if blank else 'live')
        return False

    def wake(self, name):
        dom = self.app.conn.lookupByName(name)
        # Left Shift (Linux keycode 42): harmless, and enough to wake the guest's display
        self.app.act(name, 'Waking', lambda: dom.sendKey(libvirt.VIR_KEYCODE_SET_LINUX, 0, [42], 1, 0))

    def stop(self):
        self._stop.set()


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
        win.set_default_size(820, 760)
        win.set_icon_name('computer')
        hb = Gtk.HeaderBar(show_close_button=True, title='Lab Manager')
        self.header = hb
        self.start_lab_btn = text_button('Start lab', 'accent', self.start_lab)
        self.start_lab_btn.set_tooltip_text('Start DC1, GW1, CM1, then CLIENT1–4, each after the previous is up')
        hb.pack_start(self.start_lab_btn)
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
        if load_settings().get('view') == 'consoles':
            win.set_default_size(1180, 820)
            win.resize(1180, 820)
            self.view_stack.set_visible_child_name('consoles')

    def on_view_changed(self, *_):
        view = self.view_stack.get_visible_child_name()
        self.grid.active = view == 'consoles'
        if self.grid.active:
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
        running = sum(1 for r in self.rows.values() if r.running)
        run = self.lab_run
        subtitle = run['status'] if run else f'corp.contoso.com · {running} of {len(self.rows)} running'
        if self.header.get_subtitle() != subtitle:
            self.header.set_subtitle(subtitle)
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
        if off:
            return [('Start', lambda: self.act(name, 'Starting', dom.create, toaster=toaster), True, 'Power on the VM')]
        return [
            ('header', 'Safe — Windows shuts down cleanly'),
            ('Shut down', lambda: self.shutdown(name, toaster), running, 'Saves work, closes apps, then powers off'),
            ('Restart', lambda: self.restart(name, toaster), running, 'Windows restarts itself'),
            None,
            ('header', 'Hard — like the physical buttons'),
            ('Power off', lambda: self.hard(name, parent, toaster, 'off'), True, 'Cuts power instantly; unsaved work is lost'),
            ('Reset', lambda: self.hard(name, parent, toaster, 'reset'), running, 'Instant reboot without shutting down'),
        ]

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
        run = self.lab_run = {'cancel': threading.Event(), 'touched': set(), 'status': 'Starting lab…'}
        self.start_lab_btn.set_label('Cancel')
        doms = {d.name(): d for d in self.conn.listAllDomains()}
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
