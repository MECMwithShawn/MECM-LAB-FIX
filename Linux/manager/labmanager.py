#!/usr/bin/env python3
"""Lab Manager: a Windows 11-style window for the hydration lab VMs (libvirt qemu:///system).

Main window: VM cards with role, status, live CPU graph and Start / Shut down / Restart /
Force off / Checkpoints / Connect. "Start lab" boots DC1 -> GW1 -> CM1 -> CLIENT1-4 in order,
shows its progress, can be cancelled, and skips any VM you start or stop yourself meanwhile.
Console: a slim header (VM name, Ctrl+Alt+Del, power, full screen) over the SPICE display,
which resizes the guest to the window (needs QXL graphics + the SPICE agent in the guest).

Run: python3 labmanager.py            (or: labmanager.py --console HYD-DC1)
Needs: python3-gi, gir1.2-spiceclientgtk-3.0, python3-libvirt (all pulled in by virt-manager),
qemu-utils for checkpoints, and membership of the libvirt group.
"""
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
from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Gtk, Pango, SpiceClientGLib, SpiceClientGtk  # noqa: E402
import libvirt  # noqa: E402

URI = 'qemu:///system'
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
.menu-item { padding: 6px 12px; border-radius: 4px; }
.toast {
    background: #2d2d2d; color: #f2f2f2; border-radius: 8px; padding: 10px 16px;
    border: 1px solid rgba(255,255,255,0.08); margin: 16px;
}
.placeholder { color: #9a9a9a; font-size: 14px; }
.placeholder-title { color: #ffffff; font-size: 18px; font-weight: 600; }
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
    """items: list of (label, callback, sensitive) or None for a separator."""
    pop = Gtk.Popover.new(relative_to)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=6, spacing=2)
    for item in items:
        if item is None:
            box.pack_start(Gtk.Separator(margin_top=4, margin_bottom=4), False, False, 0)
            continue
        label, cb, sensitive = item
        b = Gtk.ModelButton(text=label)
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
        a = Gtk.Label(label=addr, xalign=0)
        a.get_style_context().add_class('vm-addr')
        for wdg in (top, r, a):
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
        self.refresh()

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
            self.icon.set_from_pixbuf(tile_pixbuf(self.kind, running))
            self.spark.set_opacity(1.0 if running else 0.0)
            self.power.set_tooltip_text('Shut down' if running else 'Start')
            self.state = state
            if not running:
                self.spark.clear()
                self.last = None
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
        if self.running:
            self.app.shutdown(self.name)
        else:
            self.app.act(self.name, 'Starting', self.dom.create)

    def show_menu(self):
        off = self.state == libvirt.VIR_DOMAIN_SHUTOFF
        items = [
            ('Start', lambda: self.app.act(self.name, 'Starting', self.dom.create), off),
            ('Shut down', lambda: self.app.shutdown(self.name), self.running),
            ('Restart', lambda: self.app.act(self.name, 'Restarting', lambda: self.dom.reboot(0)), self.running),
            ('Turn off (force)', self.force_off, not off),
            None,
            ('Checkpoints…', lambda: CheckpointDialog(self.app.window, self.dom, self.state).run_dialog(), True),
            ('Open in virt-manager', lambda: subprocess.Popen(
                ['virt-manager', '--connect', URI, '--show-domain-console', self.name]), True),
        ]
        popover_menu(self.more, items).popup()

    def force_off(self):
        dlg = Gtk.MessageDialog(transient_for=self.app.window, modal=True,
                                message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK_CANCEL,
                                text=f'Turn off {self.name} without shutting down?')
        dlg.format_secondary_text('Like pulling the plug: unsaved work in the VM is lost.')
        if dlg.run() == Gtk.ResponseType.OK:
            self.app.act(self.name, 'Turning off', self.dom.destroy)
        dlg.destroy()


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
        self.session = self.display = None
        self.is_fullscreen = False

        hb = Gtk.HeaderBar(show_close_button=True, title=self.name.replace('HYD-', ''))
        hb.set_subtitle(LAB.get(self.name, ('', '', ''))[1])
        self.cad = text_button('Ctrl+Alt+Del', 'subtle', self.send_cad)
        hb.pack_start(self.cad)
        self.fs_btn = icon_button('view-fullscreen-symbolic', 'Full screen', self.toggle_fullscreen)
        self.power_btn = icon_button('system-shutdown-symbolic', 'Power')
        self.power_btn.connect('clicked', lambda *_: self.power_menu(self.power_btn))
        hb.pack_end(self.fs_btn)
        hb.pack_end(self.power_btn)
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
        for w in (self.ph_title, self.ph_text, self.ph_start):
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
        self._poll = GLib.timeout_add(1000, self.tick)
        self.show_all()
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
        self.cad.set_sensitive(running and self.display is not None)
        if running and self.session is None:
            self.open_session()
        elif not running:
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
        self.session = SpiceClientGLib.Session()
        self.session.set_property('uri', uri)
        self.session.set_property('enable-audio', False)
        gtk_session = SpiceClientGtk.GtkSession.get(self.session)
        gtk_session.set_property('auto-clipboard', True)
        GObject.GObject.connect(self.session, 'channel-new', self.on_channel)
        self.session.connect()

    def on_channel(self, _session, channel):
        if isinstance(channel, SpiceClientGLib.MainChannel):
            GObject.GObject.connect(channel, 'channel-event', self.on_main_event)
        if isinstance(channel, SpiceClientGLib.DisplayChannel) and self.display is None:
            cid = channel.get_property('channel-id')
            self.display = SpiceClientGtk.Display.new(self.session, cid)
            self.display.set_property('resize-guest', True)
            self.display.set_property('scaling', True)
            self.display.set_property('grab-keyboard', True)
            self.display.set_property('grab-mouse', False)
            self.display.show()
            self.stack.add_named(self.display, 'display')
            self.stack.set_visible_child_name('display')
            self.display.grab_focus()

    def on_main_event(self, _channel, event):
        if event in (SpiceClientGLib.ChannelEvent.CLOSED, SpiceClientGLib.ChannelEvent.ERROR_CONNECT,
                     SpiceClientGLib.ChannelEvent.ERROR_IO, SpiceClientGLib.ChannelEvent.ERROR_LINK):
            GLib.idle_add(self.close_session)

    def close_session(self):
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
        running = state == libvirt.VIR_DOMAIN_RUNNING
        popover_menu(widget, [
            ('Start', self.start_vm, state == libvirt.VIR_DOMAIN_SHUTOFF),
            ('Shut down', lambda: self.app.shutdown(self.name, toaster=self.toast), running),
            ('Restart', lambda: self.app.act(self.name, 'Restarting', lambda: self.dom.reboot(0), toaster=self.toast), running),
        ]).popup()

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


class LabManager:
    def __init__(self):
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
        overlay.add(sw)
        win.add(overlay)
        win.connect('destroy', lambda *_: (setattr(self, 'window', None), self.maybe_quit()))
        self.window = win
        GLib.timeout_add(2000, self.tick)
        self.tick()
        win.show_all()

    def tick(self):
        if self.window is None:
            return False
        for row in self.rows.values():
            row.refresh()
        running = sum(1 for r in self.rows.values() if r.running)
        run = self.lab_run
        self.header.set_subtitle(run['status'] if run else f'corp.contoso.com · {running} of {len(self.rows)} running')
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

    def shutdown(self, name, toaster=None):
        """Ask the guest to shut down: guest agent if it has one, else the ACPI power button.
        Windows ignores the power button while it's still booting, so check back later."""
        toaster = toaster or self.toast
        dom = self.conn.lookupByName(name)
        flags = libvirt.VIR_DOMAIN_SHUTDOWN_GUEST_AGENT | libvirt.VIR_DOMAIN_SHUTDOWN_ACPI_POWER_BTN
        self.act(name, 'Shutting down', lambda: dom.shutdownFlags(flags), toaster=toaster)

        def check():
            try:
                if dom.info()[0] == libvirt.VIR_DOMAIN_RUNNING:
                    toaster.show(f'{name.replace("HYD-", "")} is still running. Windows ignores shut down while it\'s '
                                 'starting up or busy; try again, or use Turn off (force).', 8)
            except libvirt.libvirtError:
                pass
            return False
        GLib.timeout_add_seconds(90, check)

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

    def maybe_quit(self):
        if self.window is None and not self.consoles:
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
    app = LabManager()
    if len(sys.argv) == 3 and sys.argv[1] == '--console':
        app.open_console(sys.argv[2])
    else:
        app.build_main()
    Gtk.main()


if __name__ == '__main__':
    main()
