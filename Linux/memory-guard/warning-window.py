#!/usr/bin/env python3
"""Always-on-top warning window for the VM memory guard.

Usage: warning-window.py '<json>'
  {"title": "...", "headline": "...", "detail": "...", "level": "warning"|"critical",
   "actions": [{"key": "pause", "label": "Pause client VMs",
                "desc": "...", "recommended": true}, ...]}

Prints the chosen action key on stdout (nothing if the window is closed).
Runs under XWayland so it can stay above other windows.
"""
import json
import os
import sys

os.environ.setdefault("GDK_BACKEND", "x11")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

CSS = b"""
window.guard, window.guard decoration { background: #1b2733; border: 1px solid #3b4a5a; }
.guard .bar { background-color: #2a1f1f; border-bottom: 1px solid #5a2a2a; padding: 10px 14px; }
.guard .bar.critical { background-color: #4a1616; }
.guard .icon { color: #ffcc00; font-size: 20px; }
.guard .title { color: #ff5b5b; font-weight: 800; font-size: 15px; }
.guard .headline { color: #ffffff; font-weight: 700; font-size: 15px; }
.guard .detail { color: #c9d3dd; font-size: 13px; }
.guard .desc { color: #aab6c2; font-size: 12px; }
.guard .tag { color: #ffcc00; font-weight: 700; font-size: 11px; }
window.guard button { background: #2c3b4b; color: #ffffff;
                border: 1px solid #44566a; border-radius: 6px; padding: 9px 10px;
                font-weight: 600; box-shadow: none; text-shadow: none; }
window.guard button:hover { background: #38495c; }
window.guard button.recommended { background: #e0a800; border-color: #ffcc00; color: #14181d; }
window.guard button.recommended:hover { background: #f2bb1a; }
window.guard button.recommended label { color: #14181d; }
window.guard button.close { background: transparent; border: none; color: #c9d3dd;
                      padding: 2px 8px; font-size: 15px; }
window.guard button.close:hover { background: #5a2a2a; }
"""


def label(text, css, wrap_chars=None, xalign=0.0):
    widget = Gtk.Label(label=text, xalign=xalign)
    widget.get_style_context().add_class(css)
    if wrap_chars:
        widget.set_line_wrap(True)
        widget.set_max_width_chars(wrap_chars)
        widget.set_width_chars(wrap_chars)
        widget.set_justify(Gtk.Justification.CENTER if xalign == 0.5 else Gtk.Justification.LEFT)
    return widget


def main():
    spec = json.loads(sys.argv[1])
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)

    win = Gtk.Window(title=spec["title"])
    win.get_style_context().add_class("guard")
    win.set_decorated(False)
    win.set_resizable(False)
    win.set_keep_above(True)
    win.stick()
    win.set_skip_taskbar_hint(True)
    win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
    win.connect("destroy", Gtk.main_quit)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    win.add(outer)

    bar = Gtk.Box(spacing=10)
    bar.get_style_context().add_class("bar")
    if spec.get("level") == "critical":
        bar.get_style_context().add_class("critical")
    bar.pack_start(label("⚠", "icon"), False, False, 0)
    bar.pack_start(label(spec["title"], "title"), True, True, 0)
    close = Gtk.Button(label="✕")
    close.get_style_context().add_class("close")
    close.set_tooltip_text("Close without doing anything")
    close.connect("clicked", lambda *_: win.destroy())
    bar.pack_end(close, False, False, 0)
    outer.pack_start(bar, False, False, 0)

    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    body.set_border_width(14)
    body.pack_start(label(spec["headline"], "headline"), False, False, 0)
    if spec.get("detail"):
        body.pack_start(label(spec["detail"], "detail", wrap_chars=70), False, False, 0)
    outer.pack_start(body, False, False, 0)

    row = Gtk.Box(spacing=12, homogeneous=True)
    row.set_border_width(14)
    row.set_margin_top(0)
    for action in spec["actions"]:
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        button = Gtk.Button(label=action["label"])
        if action.get("recommended"):
            button.get_style_context().add_class("recommended")

        def clicked(_btn, key=action["key"]):
            print(key, flush=True)
            win.destroy()

        button.connect("clicked", clicked)
        col.pack_start(button, False, False, 0)
        col.pack_start(label("RECOMMENDED" if action.get("recommended") else " ", "tag",
                             xalign=0.5), False, False, 0)
        col.pack_start(label(action.get("desc", ""), "desc", wrap_chars=24, xalign=0.5),
                       False, False, 0)
        row.pack_start(col, True, True, 0)
    outer.pack_start(row, False, False, 0)

    win.show_all()

    def place():
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_workarea()
        width, _height = win.get_size()
        win.move(area.x + (area.width - width) // 2, area.y + 40)
        win.present()
        return False

    GLib.idle_add(place)
    Gtk.main()


if __name__ == "__main__":
    main()
