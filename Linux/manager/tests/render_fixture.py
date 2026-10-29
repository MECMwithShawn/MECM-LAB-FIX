#!/usr/bin/env python3
"""Render the real GTK3 widgets on a private Xvfb display using simulated data only.

No libvirt connection, worker, guest command or VM action is permitted. This is
UI verification, not a live-lab performance or integration test.
"""
import argparse
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import sys
import time

sys.dont_write_bytecode = True


def blocked(*_args, **_kwargs):
    raise AssertionError('Live connections and VM actions are prohibited in the UI fixture')


def load_app(path):
    spec = importlib.util.spec_from_file_location('labmanager_fixture_target', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup(module, stress=False):
    m = module
    initialized, _argv = m.Gtk.init_check([])
    if not initialized:
        raise RuntimeError('GTK could not open the isolated Xvfb display')
    settings = {}
    m.load_settings = lambda: dict(settings)
    m.save_settings = lambda data: settings.update(data)
    m.libvirt.open = blocked
    m.libvirt.openReadOnly = blocked
    m.libvirt.openAuth = blocked
    m.libvirt_qemu.qemuAgentCommand = blocked
    m.run_bg = blocked
    m.GuestProbe._worker = lambda _self: None
    m.ConsoleGrid._worker = lambda _self: None
    m.ResourceView.collect = lambda _self: None
    m.ResourceView._cpu_name = staticmethod(lambda: 'Simulated 32-thread host')
    m.find_coretemp = lambda: (None, 100.0)
    m.qemu_started = lambda _name: time.time() - 7000
    m.qcow2_virtual_size = lambda _path: 511 * m.GB
    m.save_image = lambda _name: None
    for name in ('start_vm', 'start_lab', 'freeze_lab', 'open_console', 'act', 'sync_clock'):
        setattr(m.LabManager, name, blocked)
    m.ConsoleGrid.wake = blocked
    m.VmRow.sync_clock = blocked

    class Domain:
        def __init__(self, name, index):
            self._name, self.index = name, index
            self.running = index < (9 if stress else 5)
            self.ram = (4, 2, 12, 4, 4, 4, 4, 4, 4)[index] * m.GB
            self.cpus = 4 if index == 2 else 2

        def name(self):
            return self._name

        def info(self):
            state = m.libvirt.VIR_DOMAIN_RUNNING if self.running else m.libvirt.VIR_DOMAIN_SHUTOFF
            return state, self.ram // 1024, self.ram // 1024, self.cpus, 0

        def hasManagedSaveImage(self, _flags):
            return False

        def XMLDesc(self, _flags):
            agent = ('<channel><target name="org.qemu.guest_agent.0" state="connected"/></channel>'
                     if self.index < 3 else '')
            return f'''<domain><name>{self._name}</name><vcpu>{self.cpus}</vcpu>
              <currentMemory>{self.ram // 1024}</currentMemory>
              <os firmware="efi"><loader secure="yes"/></os><devices>
              <disk device="disk"><source file="/nonexistent/ui-fixture/{self._name}.qcow2"/></disk>
              <interface><mac address="52:54:00:00:00:{self.index:02x}"/>
                <source network="HYD-CorpNet"/><target dev="vnet{self.index}"/><model type="e1000e"/>
              </interface><tpm><backend version="2.0"/></tpm>
              <video><model type="qxl"/></video><graphics type="spice"/>{agent}</devices></domain>'''

        def __getattr__(self, _name):
            return blocked

    class Connection:
        def __init__(self):
            self.domains = [Domain(name, i) for i, name in enumerate(m.LAB)]

        def listAllDomains(self):
            return self.domains

        def getLibVersion(self):
            return 10000000

        def getVersion(self):
            return 8002000

        def lookupByName(self, name):
            return next(d for d in self.domains if d.name() == name)

        def __getattr__(self, _name):
            return blocked

    conn = Connection()
    m.connect = lambda: conn
    m.Gtk.Settings.get_default().set_property('gtk-application-prefer-dark-theme', True)
    m.Gtk.Settings.get_default().set_property('gtk-enable-animations', False)
    provider = m.Gtk.CssProvider()
    provider.load_from_data(m.CSS)
    m.Gtk.StyleContext.add_provider_for_screen(m.Gdk.Screen.get_default(), provider,
                                               m.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    app = m.LabManager()
    app.maybe_quit = lambda: None
    app.build_main()
    app.resources.ncpu = 32
    app.resources.temp_path = None
    app.resources.temps.clear()
    app.resources.temps.append(92)
    app.resources.disk_fs = lambda: (int(3.5 * 1024 * m.GB), int(2.2 * 1024 * m.GB))
    for i, row in enumerate(app.rows.values()):
        row.disk_used = int((26, 19, 94, 52.8, 44.5, 32.4, 31.8, .001, .001)[i] * m.GB)
        row.capacity = 511 * m.GB
        row.checkpoints = []
        if i < 3:
            info = {'os.pretty-name': 'Microsoft Windows Server 2025', 'os.kernel-release': '26100',
                    'hostname': row.name.removeprefix('HYD-'), 'user.count': int(i == 2),
                    'user.0.domain': 'CORP', 'user.0.name': 'LabAdmin', 'fs.count': 1,
                    'fs.0.mountpoint': 'C:', 'fs.0.used-bytes': int((26, 19, 94)[i] * m.GB),
                    'fs.0.total-bytes': 511 * m.GB}
            row.apply_guest({'info': info, 'ifs': {}, 'drift': 0.0, 'at': time.time()})
    app.window.set_title('Lab Manager - simulated UI validation')
    return app


def seed(module, app, samples=150):
    m, view = module, app.resources
    view.samples = 0
    view.prev = None
    for history in view.hist.values():
        for values in history.values():
            values.clear()
            values.extend([0.0] * view.HIST)
    for values in view.hhist.values():
        values.clear()
        values.extend([0.0] * view.HIST)
    if hasattr(view, 'sample_times'):
        view.sample_times.clear()
    counters = {name: dict(cpu=0.0, rd=0.0, wr=0.0, rx=0.0, tx=0.0) for name in app.rows}
    busy = total = 0
    start = time.monotonic() - samples * 2
    for index in range(samples + 1):
        q = index / max(1, samples)
        wave = .65 + .20 * math.sin(q * 17) ** 2
        burst = math.exp(-((q - .27) / .025) ** 2) + 1.4 * math.exp(-((q - .74) / .018) ** 2)
        host_pct = 8 + 8 * burst + 3 * math.sin(q * 19) ** 2
        total += 6400
        busy += round(host_pct / 100 * 6400)
        stats = {}
        for i, (name, row) in enumerate(app.rows.items()):
            dom = row.dom
            c = counters[name]
            cpu = (0.05, .06, .76, .06, .06, .08, .08, .08, .08)[i] * (wave + .6 * burst) if dom.running else 0
            c['cpu'] += cpu * 2 * 1e9
            c['rd'] += (240000 if i == 2 else 4000) * (wave + 3 * burst) * 2
            c['wr'] += (40000 if i == 2 else 1000) * wave * 2
            c['rx'] += (9000 if i == 2 else 500) * (wave + 4 * burst) * 2
            c['tx'] += (3000 if i == 2 else 100) * wave * 2
            rss = (4.18, 2.08, 12.16, 4.09, 4.09, 4.1, 4.1, 4.1, 4.1)[i] * m.GB if dom.running else 0
            s = {'state.state': dom.info()[0], 'vcpu.current': dom.cpus,
                 'balloon.current': dom.ram // 1024, 'balloon.rss': int(rss / 1024),
                 'cpu.time': int(c['cpu']), 'block.count': 1, 'net.count': 1,
                 'block.0.rd.bytes': int(c['rd']), 'block.0.wr.bytes': int(c['wr']),
                 'net.0.name': f'vnet{i}', 'net.0.rx.bytes': int(c['rx']), 'net.0.tx.bytes': int(c['tx'])}
            for core in range(dom.cpus):
                s[f'vcpu.{core}.time'] = int(c['cpu'] * .96 / dom.cpus)
            stats[name] = dom, s
        mem = {'MemTotal': int(62.5 * m.GB), 'MemAvailable': int(16.7 * m.GB),
               'SwapTotal': 0, 'SwapFree': 0}
        host = {'total': total, 'busy': busy, 'per': [(busy // 32, total // 32)] * 32,
                'mem': mem, 'load': [2.49, 2.2, 1.8], 'uptime': 35340}
        view.ingest(stats, host, start + index * 2)
    for row in app.rows.values():
        row.refresh()
    view.render()


def pump(module):
    for _ in range(40):
        while module.Gtk.events_pending():
            module.Gtk.main_iteration_do(False)
        time.sleep(.003)


def screenshot(module, app, path):
    pump(module)
    width, height = app.window.get_size()
    pix = module.Gdk.pixbuf_get_from_window(app.window.get_window(), 0, 0, width, height)
    if pix is None:
        raise RuntimeError('No pixels captured from invisible test window')
    pix.savev(str(path), 'png', [], [])


def select_chart_control(module, view, label):
    """Exercise the presentation control so its selected state matches the chart."""
    def widgets(parent):
        yield parent
        if isinstance(parent, module.Gtk.Container):
            for child in parent.get_children():
                yield from widgets(child)

    matches = [widget for widget in widgets(view)
               if isinstance(widget, module.Gtk.RadioButton) and widget.get_label() == label]
    if len(matches) != 1:
        raise AssertionError(f'Expected one chart control labeled {label!r}, got {len(matches)}')
    matches[0].set_active(True)
    mode = {'CPU': 'cpu', 'Memory': 'mem', 'Disk': 'disk', 'Network': 'net'}.get(label)
    if mode is not None:
        assert view.timeline.mode == mode
    else:
        assert view.timeline.minutes == int(label.split()[0])


def console_placeholders(module, app):
    m = module
    app.grid.refresh_states()
    for name, tile in app.grid.tiles.items():
        if not app.rows[name].running:
            continue
        surface = m.cairo.ImageSurface(m.cairo.FORMAT_ARGB32, 352, 220)
        cr = m.cairo.Context(surface)
        cr.set_source_rgb(.055, .075, .11)
        cr.paint()
        cr.select_font_face('Sans')
        cr.set_font_size(16)
        cr.set_source_rgb(.65, .77, .9)
        cr.move_to(70, 102)
        cr.show_text(name.removeprefix('HYD-') + ' - simulated console')
        cr.set_font_size(12)
        cr.move_to(100, 128)
        cr.show_text('No VM connection')
        data = io.BytesIO()
        surface.write_to_png(data)
        app.grid._show(name, data.getvalue())


def bench(function, n=40):
    values = []
    for _ in range(n):
        start = time.perf_counter()
        function()
        values.append((time.perf_counter() - start) * 1000)
    values.sort()
    return {'median_ms': round(statistics.median(values), 3),
            'p95_ms': round(values[int(.95 * (n - 1))], 3), 'max_ms': round(max(values), 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=Path(__file__).resolve().parents[1] / 'labmanager.py')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--width', type=int, default=1180)
    parser.add_argument('--height', type=int, default=1000)
    parser.add_argument('--stress', action='store_true', help='Also measure uncached 450-sample draws with nine simulated running VMs')
    args = parser.parse_args()
    if os.environ.get('LAB_MANAGER_FIXTURE_DISPLAY') != '1':
        binary = shutil.which('xvfb-run')
        if not binary:
            parser.error('Install xvfb and xauth to use the isolated screenshot fixture')
        os.environ['LAB_MANAGER_FIXTURE_DISPLAY'] = '1'
        os.environ['GDK_BACKEND'] = 'x11'
        os.execv(binary, [binary, '-a', '-s', '-screen 0 1920x1600x24 -nolisten tcp',
                         sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
    args.output.mkdir(parents=True, exist_ok=True)
    callback_errors = []
    original_hook = sys.excepthook
    def callback_exception(*exc):
        callback_errors.append(str(exc[1]))
        original_hook(*exc)
    sys.excepthook = callback_exception
    m = load_app(args.app)
    app = setup(m, stress=args.stress)
    seed(m, app)
    app.window.resize(args.width, args.height)
    pump(m)
    screenshot(m, app, args.output / 'list.png')
    app.rows['HYD-CM1'].set_expanded(True)
    screenshot(m, app, args.output / 'list-expanded.png')
    app.rows['HYD-CM1'].set_expanded(False)
    app.view_stack.set_visible_child_name('resources')
    app.window.resize(args.width, args.height)
    app.resources.active = True
    for mode, label in (('cpu', 'CPU'), ('mem', 'Memory'), ('disk', 'Disk'), ('net', 'Network')):
        select_chart_control(m, app.resources, label)
        app.resources.render()
        screenshot(m, app, args.output / f'resources-{mode}.png')
    select_chart_control(m, app.resources, 'CPU')
    app.resources.timeline._hover(app.resources.timeline.get_allocated_width() * .74)
    screenshot(m, app, args.output / 'resources-hover.png')
    app.resources.timeline._hover(None)
    select_chart_control(m, app.resources, '15 min')
    screenshot(m, app, args.output / 'resources-15min.png')
    select_chart_control(m, app.resources, '5 min')
    seed(m, app, samples=3)
    screenshot(m, app, args.output / 'resources-startup.png')
    seed(m, app, samples=0)
    screenshot(m, app, args.output / 'resources-empty.png')
    seed(m, app, samples=1)
    screenshot(m, app, args.output / 'resources-one-sample.png')
    seed(m, app)
    app.resources.render()
    pump(m)
    adjustment = app.resources.get_vadjustment()
    adjustment.set_value(max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size()))
    screenshot(m, app, args.output / 'resources-details.png')
    adjustment.set_value(adjustment.get_lower())
    pump(m)
    chart = app.resources.timeline
    surface = m.cairo.ImageSurface(m.cairo.FORMAT_ARGB32, chart.get_allocated_width(), chart.get_allocated_height())
    cr = m.cairo.Context(surface)
    measurements = {'scope': 'simulated fixture; excludes libvirt collection and real console image decoding',
                    'samples': app.resources.samples, 'tick': bench(app.tick),
                    'resource_render': bench(app.resources.render),
                    'timeline_draw': bench(lambda: chart._draw(chart, cr))}
    if args.stress:
        seed(m, app, samples=450)
        select_chart_control(m, app.resources, '15 min')
        measurements['uncached_9vm_450_samples'] = {}
        for mode, label in (('cpu', 'CPU'), ('mem', 'Memory'), ('disk', 'Disk'), ('net', 'Network')):
            select_chart_control(m, app.resources, label)
            pump(m)
            def uncached():
                chart._cache_key = None
                chart._draw(chart, cr)
            measurements['uncached_9vm_450_samples'][mode] = bench(uncached)
    measurements['window_size'] = list(app.window.get_size())
    app.view_stack.set_visible_child_name('consoles')
    console_placeholders(m, app)
    screenshot(m, app, args.output / 'consoles.png')
    (args.output / 'measurements.json').write_text(json.dumps(measurements, indent=2) + '\n')
    print(json.dumps(measurements))
    app.window.destroy()
    if callback_errors:
        raise RuntimeError('GTK callback errors: ' + '; '.join(callback_errors))


if __name__ == '__main__':
    main()
