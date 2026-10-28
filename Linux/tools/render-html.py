"""Render an HTML page to a full-page PNG with WebKitGTK (used for docs/lab-reference.png).

Usage: render-html.py in.html out.png [zoom]     (zoom 2 = 2x pixels for sharp printing)
Needs gir1.2-webkit2-4.1. Runs on the desktop; to render without a window popping up, use a
private X server:  xvfb-run -a python3 render-html.py ../docs/lab-reference.html ../docs/lab-reference.png 2
"""
import sys

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gio, GLib, Gtk, WebKit2  # noqa: E402

src, out = sys.argv[1], sys.argv[2]
zoom = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
win = Gtk.OffscreenWindow()
view = WebKit2.WebView()
view.set_zoom_level(zoom)
view.get_settings().set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
win.add(view)
win.set_default_size(int(1600 * zoom), int(1200 * zoom))
win.show_all()


def snap():
    def got(v, res):
        surf = v.get_snapshot_finish(res)
        surf.write_to_png(out)
        print('wrote', out, surf.get_width(), 'x', surf.get_height())
        Gtk.main_quit()
    view.get_snapshot(WebKit2.SnapshotRegion.FULL_DOCUMENT, WebKit2.SnapshotOptions.NONE, None, got)
    return False


def loaded(v, event):
    if event == WebKit2.LoadEvent.FINISHED:
        GLib.timeout_add(1500, snap)


view.connect('load-changed', loaded)
view.load_uri(Gio.File.new_for_path(src).get_uri())
GLib.timeout_add_seconds(60, Gtk.main_quit)
Gtk.main()
