#!/usr/bin/env python3
"""Lab Manager: a Windows 11-style window for the hydration lab VMs (libvirt qemu:///system).

Main window, three views:
- List: VM cards with status, role, Windows version, live CPU / memory / I/O (or when it was last
  used / frozen), a facts strip (addresses, signed in, uptime, C:, checkpoints), warning chips
  (clock drift, full C:, guest tools not answering), an expandable details panel, Connect, and
  VMware-style power options: safe Shut down / Restart (through Windows) and hard Power off / Reset
  (like the physical buttons, confirmed first). Windows details come from the QEMU guest agent
  (GuestProbe, about once a minute), so VMs without guest tools show the lab plan instead.
- Consoles: live previews of every VM from libvirt screenshots, which don't take the VM's one SPICE
  viewer slot; click one to open its console.
- Resources: a dashboard of what the lab costs the host: rings for CPU / memory / storage, a
  verdict, capacity, host threads, KPIs, insights with actions, an activity timeline per VM, where
  host RAM goes, a per-VM table, the lab networks and host facts.
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
import math
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('SpiceClientGtk', '3.0')
gi.require_version('SpiceClientGLib', '2.0')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango, PangoCairo, SpiceClientGLib, SpiceClientGtk  # noqa: E402
import cairo  # noqa: E402
import libvirt  # noqa: E402
import libvirt_qemu  # noqa: E402

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
.res-main { font-size: 13px; color: #f0f0f0; font-feature-settings: "tnum"; }
.res-total { font-size: 13px; color: #60cdff; font-weight: 600; font-feature-settings: "tnum"; }
.res-sub { font-size: 11px; color: #9a9a9a; font-feature-settings: "tnum"; }
/* ---- List: richer VM cards ---- */
.card.vm { padding: 12px 14px 10px 14px; margin: 0 2px 8px 2px; border-top-color: rgba(255,255,255,0.09);
    box-shadow: 0 1px 2px rgba(0,0,0,0.30); transition: background-color 120ms ease-out, border-color 120ms ease-out; }
.card.vm:hover { background: #2f2f2f; border-color: rgba(255,255,255,0.10); }
.card.vm.off .facts { opacity: 0.6; }
.os-chip { border-radius: 4px; padding: 0 6px; font-size: 11px; color: #c8c8c8; font-feature-settings: "tnum";
    background-color: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.06); }
.os-chip.none { color: #8a8a8a; background-color: rgba(255,255,255,0.03); }
.mini-cap { font-size: 10px; font-weight: 600; color: #8a8a8a; }
.mini-val { font-size: 13px; color: #f0f0f0; font-feature-settings: "tnum"; }
.stat { font-size: 11px; color: #c8c8c8; font-feature-settings: "tnum"; }
.off-sum { font-size: 12px; color: #c8c8c8; font-feature-settings: "tnum"; }
.off-sub { font-size: 11px; color: #8a8a8a; font-feature-settings: "tnum"; }
.facts { margin: 10px 0 0 52px; padding-top: 8px; border-top: 1px solid rgba(255,255,255,0.05); }
.fact-cap { font-size: 10px; font-weight: 600; color: #8a8a8a; }
.fact-val { font-size: 12px; color: #e6e6e6; font-feature-settings: "tnum"; }
.fact-val.muted { color: #7e7e7e; }
.fact-val.dim-italic { color: #7e7e7e; font-style: italic; }
.fact-sep { min-width: 1px; background-color: rgba(255,255,255,0.06); margin: 2px 10px; }
.chips { margin: 8px 0 0 52px; }
.chip { border-radius: 999px; padding: 1px 8px; font-size: 11px; font-weight: 600; }
button.chip { border: none; box-shadow: none; min-height: 0; }
.chip.warn  { background-color: rgba(252,225,0,0.14); color: #fce100; }
.chip.error { background-color: rgba(255,153,164,0.16); color: #ff99a4; }
button.chip.warn:hover  { background-color: rgba(252,225,0,0.24); }
button.chip.error:hover { background-color: rgba(255,153,164,0.26); }
button.chevron { background: none; border: none; box-shadow: none; border-radius: 6px; min-width: 30px; min-height: 30px;
    padding: 0; color: #a8a8a8; }
button.chevron:hover { background-color: rgba(255,255,255,0.08); color: #ffffff; }
.vm-detail { background-color: rgba(0,0,0,0.18); border: 1px solid rgba(255,255,255,0.04); border-radius: 6px;
    padding: 12px 14px; margin: 10px 0 0 52px; }
.detail-head { font-size: 11px; font-weight: 600; color: #8a8a8a; margin-bottom: 4px; }
.detail-key { font-size: 11px; color: #8a8a8a; }
.detail-val { font-size: 12px; color: #e6e6e6; font-feature-settings: "tnum"; }
.detail-val.bad { color: #ff99a4; }
.detail-val.muted { color: #7e7e7e; }
button.link { background: none; border: none; box-shadow: none; padding: 0; min-height: 0; color: #60cdff; font-size: 12px; }
button.link:hover { color: #99ebff; }

/* ---- Resources: hero ---- */
.hero { background-color: #2a2b2e;
    background-image: radial-gradient(circle farthest-side at left top, rgba(96,205,255,0.12), rgba(96,205,255,0)),
                      linear-gradient(to bottom, #2f3033, #27282a);
    border: 1px solid rgba(255,255,255,0.07); border-top-color: rgba(255,255,255,0.13); border-radius: 12px;
    padding: 20px 24px; margin: 12px 4px 12px 4px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.30), 0 8px 20px rgba(0,0,0,0.22); }
.ring-value { font-size: 24px; font-weight: 600; color: #ffffff; font-feature-settings: "tnum"; }
.ring-unit { font-size: 11px; color: #9a9a9a; }
.ring-title { font-size: 12px; font-weight: 600; color: #d0d0d0; }
.ring-legend { font-size: 11px; color: #9a9a9a; font-feature-settings: "tnum"; }
.vhair { min-width: 1px; margin: 8px 20px;
    background-image: linear-gradient(to bottom, rgba(255,255,255,0), rgba(255,255,255,0.10) 20%, rgba(255,255,255,0.10) 80%, rgba(255,255,255,0)); }
.hero-title { font-size: 20px; font-weight: 600; color: #ffffff; }
.hero-sub { font-size: 12px; color: #a8a8a8; font-feature-settings: "tnum"; }
.eyebrow { font-size: 10px; font-weight: 600; color: #8a8a8a; }
.headroom-num { font-size: 34px; font-weight: 600; color: #ffffff; font-feature-settings: "tnum"; }
.headroom-num.warn { color: #fce100; }
.headroom-num.crit { color: #ff99a4; }
.headroom-text { font-size: 14px; color: #d0d0d0; }
.kpi { background-color: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.05); border-radius: 6px;
    padding: 8px 10px; }
.kpi-cap { font-size: 10px; font-weight: 600; color: #8a8a8a; }
.kpi-val { font-size: 15px; font-weight: 600; color: #f2f2f2; font-feature-settings: "tnum"; }
.kpi-val.warn { color: #fce100; }

/* ---- Resources: cards, sections ---- */
.card.res { border-radius: 10px; padding: 14px 16px; margin: 0 4px 12px 4px; border-top-color: rgba(255,255,255,0.10);
    box-shadow: 0 1px 3px rgba(0,0,0,0.28), 0 4px 12px rgba(0,0,0,0.14); }
.card.res:hover { background: #2b2b2b; }
.card.res.listgroup { padding: 0; }
.section-cap { font-size: 11px; color: #8a8a8a; margin: 18px 2px 8px 2px; font-feature-settings: "tnum"; }
.scale-tag { font-size: 10px; color: #8a8a8a; background-color: rgba(255,255,255,0.06); border-radius: 4px; padding: 1px 6px; }
.seg { background-color: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.06); border-radius: 6px; padding: 2px; }
.seg radiobutton, .seg button { background: none; border: none; box-shadow: none; border-radius: 4px; padding: 2px 12px;
    min-height: 22px; font-size: 11px; color: #b0b0b0; transition: background-color 150ms ease-out; }
.seg radiobutton:hover, .seg button:hover { background-color: rgba(255,255,255,0.06); }
.seg radiobutton:checked, .seg button:checked { background-color: rgba(96,205,255,0.16); color: #60cdff; font-weight: 600; }
.legend-chip { background-color: rgba(255,255,255,0.04); border: none; box-shadow: none; border-radius: 999px;
    padding: 1px 10px 1px 8px; min-height: 0; font-size: 11px; color: #c8c8c8; font-feature-settings: "tnum"; }
.legend-chip:hover { background-color: rgba(255,255,255,0.08); }
.legend-chip:checked { background-color: rgba(255,255,255,0.12); color: #ffffff; }

/* ---- list groups: insights + VM table ---- */
.lg-head { padding: 8px 16px; background-color: rgba(255,255,255,0.02); border-radius: 10px 10px 0 0; }
.lg-head label { font-size: 10px; font-weight: 600; color: #8a8a8a; }
.lg-sep { min-height: 1px; background-color: rgba(255,255,255,0.05); }
.insight { padding: 12px 16px; min-height: 44px; }
.insight-title { font-size: 13px; font-weight: 600; color: #f0f0f0; }
.insight-body { font-size: 12px; color: #b8b8b8; }
.res-row { padding: 8px 16px; transition: background-color 100ms ease-out; }
.res-row:hover, .res-row.hl { background-color: rgba(255,255,255,0.04); }
.res-row.focused { background-color: rgba(96,205,255,0.06); }
.res-row.off { opacity: 0.6; padding-top: 4px; padding-bottom: 4px; }
.res-name { font-size: 13px; font-weight: 600; color: #ffffff; }
.res-total-row { padding: 10px 16px; background-color: rgba(96,205,255,0.05);
    border-top: 1px solid rgba(96,205,255,0.20); border-radius: 0 0 10px 10px; }

/* ---- networks + footer ---- */
.net-title { font-size: 13px; font-weight: 600; color: #f0f0f0; }
.net-tag { background-color: rgba(255,255,255,0.06); border-radius: 4px; padding: 1px 8px; font-size: 11px; color: #a8a8a8; }
.net-rate { font-size: 18px; font-weight: 600; color: #ffffff; font-feature-settings: "tnum"; }
.net-member { font-size: 11px; color: #9a9a9a; }
.footer { background-color: #262626; border: 1px solid rgba(255,255,255,0.05); border-radius: 8px; padding: 10px 16px;
    margin: 0 4px 8px 4px; }
.foot-key { font-size: 11px; color: #8a8a8a; }
.foot-val { font-size: 12px; color: #d8d8d8; font-feature-settings: "tnum"; }
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


# -- data helpers ---------------------------------------------------------
GB = 1 << 30
CLK_TCK = os.sysconf('SC_CLK_TCK')
SAVE_DIR = '/var/lib/libvirt/qemu/save'

# One colour per VM, used only in the Resources view (charts, swatches); the List stays accent-only
VM_COLORS = {
    'HYD-DC1': '#8B9CFF', 'HYD-GW1': '#3DD6C4', 'HYD-CM1': '#FFB35C',
    'HYD-CLIENT1': '#F0866C', 'HYD-CLIENT2': '#E879F9', 'HYD-CLIENT3': '#C5D86D', 'HYD-CLIENT4': '#FF6FAE',
    'HYD-Client5': '#D6B38A', 'HYD-Client6': '#94A3B8',
}


def rgb(hexstr):
    h = hexstr.lstrip('#')
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def vm_rgb(name):
    if name in VM_COLORS:
        return rgb(VM_COLORS[name])
    import colorsys  # VMs outside the lab plan: golden-angle hues
    h = (sum(map(ord, name)) * 0.618034) % 1
    return colorsys.hls_to_rgb(h, 0.68, 0.7)


def vm_hex(name):
    return '#%02x%02x%02x' % tuple(int(c * 255) for c in vm_rgb(name))


def short(name):
    return name.replace('HYD-', '')


def fmt_bytes(n, digits=1):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.{digits}f} {unit}'
        n /= 1024


def fmt_rate(n):
    return fmt_bytes(max(0, n)) + '/s'


def fmt_span(seconds):
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f'{d} d {h} h'
    if h:
        return f'{h} h {m} m'
    return f'{m} m' if m else 'just now'


def fmt_ago(epoch):
    if not epoch:
        return ''
    span = time.time() - epoch
    if span < 60:
        return 'just now'
    if span > 7 * 86400:
        return time.strftime('%b %-d', time.localtime(epoch))
    return fmt_span(span) + ' ago'


def fmt_drift(sec):
    s = abs(sec)
    return f'{int(s // 60)} m {int(s % 60)} s' if s >= 60 else f'{s:.1f} s'


def nice_ceil(x):
    """Smallest 1/2/5 x 10^n that is >= x."""
    if x <= 0:
        return 1
    p = 10 ** int(f'{x:e}'.split('e')[1])
    for m in (1, 2, 5, 10):
        if m * p >= x:
            return m * p
    return 10 * p


WIN_RELEASE = {19045: '22H2', 22000: '21H2', 22621: '22H2', 22631: '23H2', 26100: '24H2', 26200: '25H2'}


def os_label(info):
    """'Win 11 24H2 · 26100' / 'Server 2025 · 26100' from guestInfo OS keys."""
    pretty = info.get('os.pretty-name', '')
    try:
        build = int(info.get('os.kernel-release') or 0)
    except ValueError:
        build = 0
    if not pretty:
        return ''
    if 'Server' in pretty:
        name = {17763: 'Server 2019', 20348: 'Server 2022', 26100: 'Server 2025'}.get(build) or \
            re.sub(r'.*(Server \d{4}).*', r'\1', pretty)
        return f'{name} · {build}' if build else name
    release = WIN_RELEASE.get(build, '')
    win = 'Win 11' if build >= 22000 else 'Win 10'
    return f'{win} {release} · {build}'.replace('  ', ' ') if build else pretty


def read_boot_time():
    with open('/proc/stat') as f:
        for line in f:
            if line.startswith('btime'):
                return int(line.split()[1])
    return 0


BOOT_TIME = read_boot_time()


def read_host():
    """Host CPU counters (total and per thread), memory, load and uptime from /proc."""
    cpus = {}
    with open('/proc/stat') as f:
        for line in f:
            if not line.startswith('cpu'):
                break
            p = line.split()
            v = [int(x) for x in p[1:9]]
            cpus[p[0]] = (sum(v) - v[3] - v[4], sum(v))
    mem = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, val = line.split(':')
            mem[k] = int(val.split()[0]) * 1024
    with open('/proc/loadavg') as f:
        load = [float(x) for x in f.read().split()[:3]]
    with open('/proc/uptime') as f:
        uptime = float(f.read().split()[0])
    per = [cpus[k] for k in sorted((k for k in cpus if k != 'cpu'), key=lambda k: int(k[3:]))]
    return {'busy': cpus['cpu'][0], 'total': cpus['cpu'][1], 'per': per, 'mem': mem, 'load': load, 'uptime': uptime}


def find_coretemp():
    """(input path, crit °C) of the CPU package sensor, or (None, 100)."""
    base = '/sys/class/hwmon'
    try:
        for h in sorted(os.listdir(base)):
            try:
                with open(f'{base}/{h}/name') as f:
                    if f.read().strip() != 'coretemp':
                        continue
            except OSError:
                continue
            crit = 100.0
            try:
                with open(f'{base}/{h}/temp1_crit') as f:
                    crit = int(f.read()) / 1000
            except (OSError, ValueError):
                pass
            return f'{base}/{h}/temp1_input', crit
    except OSError:
        pass
    return None, 100.0


def qemu_started(name):
    """When the VM's QEMU process started (epoch), from its pid file; None if unknown."""
    try:
        with open(f'/run/libvirt/qemu/{name}.pid') as f:
            pid = int(f.read().strip())
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            if f'guest={name}'.encode() not in f.read():
                return None
        with open(f'/proc/{pid}/stat') as f:
            fields = f.read().rsplit(')', 1)[1].split()
        return BOOT_TIME + int(fields[19]) / CLK_TCK
    except (OSError, ValueError, IndexError):
        return None


def qcow2_virtual_size(path):
    """Virtual disk size from a qcow2 header (bytes 24-31), without libvirt or qemu-img."""
    try:
        with open(path, 'rb') as f:
            head = f.read(32)
        if head[:4] == b'QFI\xfb':
            return int.from_bytes(head[24:32], 'big')
    except OSError:
        pass
    return None


def save_image(name):
    """(size, mtime) of a frozen VM's managed-save image, or None when it can't be read."""
    try:
        st = os.stat(f'{SAVE_DIR}/{name}.save')
        return st.st_blocks * 512, st.st_mtime
    except OSError:
        return None


def parse_facts(xml):
    """Static facts about a VM from its live XML."""
    root = ET.fromstring(xml)
    f = {'vcpus': int(root.findtext('vcpu') or 0),
         'mem': int(root.findtext('currentMemory') or root.findtext('memory') or 0) * 1024,
         'nics': [], 'disk': None, 'capacity': None}
    for i in root.findall('./devices/interface'):
        src, tgt = i.find('source'), i.find('target')
        f['nics'].append({
            'mac': (i.find('mac').get('address') if i.find('mac') is not None else ''),
            'network': src.get('network') if src is not None else '',
            'vnet': tgt.get('dev') if tgt is not None else '',
            'model': i.find('model').get('type') if i.find('model') is not None else ''})
    d = root.find("./devices/disk[@device='disk']/source")
    f['disk'] = d.get('file') if d is not None else None
    os_el = root.find('os')
    loader = root.find('os/loader')
    f['efi'] = (os_el is not None and os_el.get('firmware') == 'efi') or loader is not None
    f['secure'] = loader is not None and loader.get('secure') == 'yes'
    tpm = root.find('./devices/tpm')
    f['tpm'] = (tpm.find('backend').get('version') or '2.0') if tpm is not None and tpm.find('backend') is not None else None
    v = root.find('./devices/video/model')
    f['video'] = v.get('type').upper() if v is not None else ''
    g = root.find('./devices/graphics')
    f['graphics'] = g.get('type').upper() if g is not None else ''
    t = root.find("./devices/channel/target[@name='org.qemu.guest_agent.0']")
    f['agent'] = t is not None and t.get('state') == 'connected'
    return f


NET_KIND = {'HYD-CorpNet': ('Isolated', '10.0.0.0/24', '#60cdff'), 'HYD-InterNet': ('NAT', '192.168.16.0/24', '#9a9a9a')}


# -- drawing helpers ------------------------------------------------------
def rrect(cr, x, y, w, h, r):
    r = max(0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


_HATCH = []


def hatch():
    """A repeating diagonal-line pattern (QEMU overhead, "other" load)."""
    if not _HATCH:
        s = cairo.ImageSurface(cairo.FORMAT_ARGB32, 6, 6)
        c = cairo.Context(s)
        c.set_source_rgba(1, 1, 1, 0.35)
        c.set_line_width(1)
        for a, b in (((0, 6), (6, 0)), ((-1, 1), (1, -1)), ((5, 7), (7, 5))):
            c.move_to(*a)
            c.line_to(*b)
        c.stroke()
        p = cairo.SurfacePattern(s)
        p.set_extend(cairo.EXTEND_REPEAT)
        _HATCH.append(p)
    return _HATCH[0]


def smooth_path(cr, pts, move=True):
    """Catmull-Rom through pts, as Béziers, with control points clamped so it never overshoots."""
    if not pts:
        return
    if move:
        cr.move_to(*pts[0])
    else:
        cr.line_to(*pts[0])
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        lo, hi = min(p1[1], p2[1]), max(p1[1], p2[1])
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, min(hi, max(lo, p1[1] + (p2[1] - p0[1]) / 6)))
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, min(hi, max(lo, p2[1] - (p3[1] - p1[1]) / 6)))
        cr.curve_to(*c1, *c2, *p2)


def layout(widget, text, px, bold=False):
    lay = widget.create_pango_layout(text)
    fd = widget.get_pango_context().get_font_description().copy()
    fd.set_absolute_size(px * Pango.SCALE)
    if bold:
        fd.set_weight(Pango.Weight.SEMIBOLD)
    lay.set_font_description(fd)
    return lay


def lerp_rgb(stops, t):
    """stops: [(pos, (r, g, b))]; colour at t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if t <= p1:
            k = (t - p0) / max(1e-9, p1 - p0)
            return tuple(a + (b - a) * k for a, b in zip(c0, c1))
    return stops[-1][1]


SEV_RGB = {'ok': rgb('#6CCB5F'), 'info': rgb('#60CDFF'), 'warn': rgb('#FCE100'), 'crit': rgb('#FF99A4')}


class Sparkline(Gtk.DrawingArea):
    """A small area chart. scale=100 for percentages, 'auto' to fit the data (at least 25)."""
    def __init__(self, color=ACCENT, w=96, h=28, n=40, scale=100, dot=False):
        super().__init__()
        self.set_size_request(w, h)
        self.color, self.scale, self.dot = color, scale, dot
        self.values = [0.0] * n
        self.connect('draw', self._draw)

    def push(self, v):
        self.values = self.values[1:] + [max(0.0, v if self.scale == 'auto' else min(100.0, v))]
        self.queue_draw()

    def set_values(self, values):
        n = len(self.values)
        vals = list(values)[-n:]
        self.values = [0.0] * (n - len(vals)) + vals
        self.queue_draw()

    def clear(self):
        self.values = [0.0] * len(self.values)
        self.queue_draw()

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        top = max(25.0, nice_ceil(max(self.values) * 1.1)) if self.scale == 'auto' else 100.0
        n = len(self.values)
        pad = 3 if self.dot else 2
        pts = [(i * (w - pad) / (n - 1), h - 2 - (v / top) * (h - 4)) for i, v in enumerate(self.values)]
        smooth_path(cr, pts)
        cr.line_to(pts[-1][0], h)
        cr.line_to(0, h)
        cr.close_path()
        g = cairo.LinearGradient(0, 0, 0, h)
        g.add_color_stop_rgba(0, *self.color, 0.35)
        g.add_color_stop_rgba(1, *self.color, 0.0)
        cr.set_source(g)
        cr.fill()
        smooth_path(cr, pts)
        cr.set_source_rgba(*self.color, 0.95)
        cr.set_line_width(1.4)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke()
        if self.dot:
            x, y = pts[-1]
            cr.arc(x, y, 5, 0, 2 * math.pi)
            cr.set_source_rgba(*self.color, 0.2)
            cr.fill()
            cr.arc(x, y, 2.2, 0, 2 * math.pi)
            cr.set_source_rgba(*self.color, 1)
            cr.fill()
        return False


class Meter(Gtk.DrawingArea):
    """A rounded bar split into coloured segments: [(fraction, (r, g, b, a)), ...]; optional tick."""
    def __init__(self, height=10, width=-1):
        super().__init__()
        self.set_size_request(width, height)
        self.set_valign(Gtk.Align.CENTER)
        self.segments = []
        self.tick = None
        self.connect('draw', self._draw)

    def set(self, segments, tick=None):
        if segments != self.segments or tick != self.tick:
            self.segments, self.tick = segments, tick
            self.queue_draw()

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        rrect(cr, 0, 0, w, h, h / 2)
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
        if self.tick is not None:
            cr.rectangle(round(self.tick * w) - 0.5, 0, 1, h)
            cr.set_source_rgba(1, 1, 1, 0.5)
            cr.fill()
        return False


class MirrorSpark(Gtk.DrawingArea):
    """Two series around a midline: up (read / received) above, down (write / sent) below."""
    def __init__(self, color=ACCENT, w=72, h=28, n=40, label=False):
        super().__init__()
        self.set_size_request(w, h)
        self.color, self.n, self.label = color, n, label
        self.up, self.down = [0.0] * n, [0.0] * n
        self.connect('draw', self._draw)

    def set_values(self, up, down):
        up, down = list(up)[-self.n:], list(down)[-self.n:]
        self.up = [0.0] * (self.n - len(up)) + up
        self.down = [0.0] * (self.n - len(down)) + down
        self.queue_draw()

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        mid = h / 2
        top = max(65536.0, max(self.up + self.down))
        cr.rectangle(0, round(mid) - 0.5, w, 1)
        cr.set_source_rgba(1, 1, 1, 0.08)
        cr.fill()
        for series, sign, fill_a, line_a in ((self.up, -1, 0.30, 0.95), (self.down, 1, 0.20, 0.6)):
            pts = [(i * w / (self.n - 1), mid + sign * (v / top) * (mid - 1)) for i, v in enumerate(series)]
            smooth_path(cr, pts)
            cr.line_to(w, mid)
            cr.line_to(0, mid)
            cr.close_path()
            cr.set_source_rgba(*self.color, fill_a)
            cr.fill()
            smooth_path(cr, pts)
            cr.set_source_rgba(*self.color, line_a)
            cr.set_line_width(1.2)
            cr.stroke()
        if self.label:
            lay = layout(self, f'peak {fmt_rate(max(self.up + self.down))}', 10)
            cr.move_to(w - lay.get_pixel_size()[0] - 2, 1)
            cr.set_source_rgba(0.54, 0.54, 0.54, 1)
            PangoCairo.show_layout(cr, lay)
        return False


class Swatch(Gtk.DrawingArea):
    def __init__(self, color, size=10, alpha=1.0, round_=False):
        super().__init__()
        self.set_size_request(size, size)
        self.set_valign(Gtk.Align.CENTER)
        self.color, self.alpha, self.round = color, alpha, round_
        self.connect('draw', self._draw)

    def set_alpha(self, a):
        if a != self.alpha:
            self.alpha = a
            self.queue_draw()

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        if self.round:
            cr.arc(w / 2, h / 2, min(w, h) / 2, 0, 2 * math.pi)
        else:
            rrect(cr, 0, 0, w, h, 3)
        cr.set_source_rgba(*self.color, self.alpha)
        cr.fill()
        return False


class RingGauge(Gtk.DrawingArea):
    """A 270° gauge: grey "other" arc, gradient "lab" arc with a soft glow, eased between values."""
    SIZE = 132

    def __init__(self):
        super().__init__()
        self.set_size_request(self.SIZE, self.SIZE)
        self.shown = (0.0, 0.0)
        self.target = (0.0, 0.0)
        self.anim = None
        self.stops = ('#2F8FFF', '#60CDFF', '#B5ECFF')
        self.connect('draw', self._draw)

    def set(self, lab, other, stops=None):
        target = (max(0.0, min(1.0, lab)), max(0.0, min(1.0, other)))
        if stops and stops != self.stops:
            self.stops = stops
            self.queue_draw()
        if target == self.target:
            return
        self.target = target
        if not self.get_mapped():
            self.shown = target
            return
        start, t0 = self.shown, None

        def step(_w, clock):
            nonlocal t0
            now = clock.get_frame_time() / 1e6
            t0 = t0 or now
            k = min(1.0, (now - t0) / 0.35)
            e = 1 - (1 - k) ** 3
            self.shown = tuple(a + (b - a) * e for a, b in zip(start, self.target))
            self.queue_draw()
            if k >= 1:
                self.anim = None
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE
        if self.anim:
            self.remove_tick_callback(self.anim)
        self.anim = self.add_tick_callback(step)

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        cx, cy, R = w / 2, h / 2 + 4, 54
        a0, S = 0.75 * math.pi, 1.5 * math.pi
        lab, other = self.shown
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(10)
        cr.arc(cx, cy, R, a0, a0 + S)
        cr.set_source_rgba(1, 1, 1, 0.07)
        cr.stroke()
        cr.set_line_width(1)
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        for k in (0, 0.25, 0.5, 0.75, 1):
            a = a0 + S * k
            cr.move_to(cx + (R + 8) * math.cos(a), cy + (R + 8) * math.sin(a))
            cr.line_to(cx + (R + 11) * math.cos(a), cy + (R + 11) * math.sin(a))
        cr.set_source_rgba(1, 1, 1, 0.14)
        cr.stroke()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        if lab + other > 0.003:
            cr.set_line_width(10)
            cr.arc(cx, cy, R, a0, a0 + S * min(1, lab + other))
            cr.set_source_rgba(0.62, 0.62, 0.62, 0.45)
            cr.stroke()
        if lab > 0.003:
            c0, c1, c2 = (rgb(s) for s in self.stops)
            cr.set_line_cap(cairo.LINE_CAP_BUTT)
            cr.set_line_width(18)
            cr.arc(cx, cy, R, a0, a0 + S * lab)
            cr.set_source_rgba(*c1, 0.10)
            cr.stroke()
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_width(10)
            g = cairo.LinearGradient(cx - R, cy + R, cx + R, cy - R)
            g.add_color_stop_rgb(0, *c0)
            g.add_color_stop_rgb(0.55, *c1)
            g.add_color_stop_rgb(1, *c2)
            cr.arc(cx, cy, R, a0, a0 + S * lab)
            cr.set_source(g)
            cr.stroke()
            a = a0 + S * lab
            cr.arc(cx + R * math.cos(a), cy + R * math.sin(a), 2.2, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.fill()
        return False


class ThreadMap(Gtk.DrawingArea):
    """One cell per host thread, coloured by how busy it is."""
    RAMP = [(0, rgb('#333A40')), (0.6, rgb('#60CDFF')), (1, rgb('#E6F7FF'))]

    def __init__(self, n):
        super().__init__()
        self.cols = max(1, (n + 1) // 2)
        self.values = [0.0] * n
        self.set_size_request(int(self.cols * 11.6), 20)
        self.connect('draw', self._draw)

    def set(self, values):
        self.values = values
        self.queue_draw()

    def _draw(self, _w, cr):
        cw, chh, gap = 10, 8, 1.6
        for i, v in enumerate(self.values):
            col, row = i % self.cols, i // self.cols
            x, y = col * (cw + gap), 1 + row * (chh + gap + 0.4)
            rrect(cr, x, y, cw, chh, 1.6)
            cr.set_source_rgb(*lerp_rgb(self.RAMP, v / 100))
            cr.fill_preserve()
            if v > 90:
                cr.set_source_rgba(1, 1, 1, 0.35)
                cr.set_line_width(1)
                cr.stroke()
            cr.new_path()
        return False


class TempBar(Gtk.DrawingArea):
    RAMP = ('#6CCB5F', '#FCE100', '#FF99A4')

    def __init__(self):
        super().__init__()
        self.set_size_request(-1, 3)
        self.frac = 0.0
        self.connect('draw', self._draw)

    def set(self, frac):
        frac = max(0.0, min(1.0, frac))
        if abs(frac - self.frac) > 0.002:
            self.frac = frac
            self.queue_draw()

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        rrect(cr, 0, 0, w, h, h / 2)
        cr.set_source_rgba(1, 1, 1, 0.07)
        cr.fill()
        g = cairo.LinearGradient(0, 0, w, 0)
        for pos, c in zip((0, 0.6, 1), self.RAMP):
            g.add_color_stop_rgb(pos, *rgb(c))
        rrect(cr, 0, 0, max(h, w * self.frac), h, h / 2)
        cr.set_source(g)
        cr.fill()
        return False


class StatusGlyph(Gtk.DrawingArea):
    def __init__(self, sev='ok'):
        super().__init__()
        self.set_size_request(20, 20)
        self.set_valign(Gtk.Align.START)
        self.sev = sev
        self.connect('draw', self._draw)

    def set(self, sev):
        if sev != self.sev:
            self.sev = sev
            self.queue_draw()

    def _draw(self, _w, cr):
        c = SEV_RGB.get(self.sev, SEV_RGB['info'])
        cr.arc(10, 10, 9, 0, 2 * math.pi)
        cr.set_source_rgba(*c, 0.18)
        cr.fill_preserve()
        cr.set_source_rgba(*c, 0.9)
        cr.set_line_width(1)
        cr.stroke()
        cr.set_source_rgb(0.96, 0.96, 0.96)
        cr.set_line_width(1.6)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        if self.sev == 'ok':
            cr.move_to(6, 10.5)
            cr.line_to(9, 13.5)
            cr.line_to(14.5, 7)
            cr.stroke()
        elif self.sev == 'info':
            cr.arc(10, 6.5, 1.1, 0, 2 * math.pi)
            cr.fill()
            cr.move_to(10, 9)
            cr.line_to(10, 14)
            cr.stroke()
        elif self.sev == 'warn':
            cr.move_to(10, 5.5)
            cr.line_to(10, 11)
            cr.stroke()
            cr.arc(10, 14, 1.1, 0, 2 * math.pi)
            cr.fill()
        else:
            cr.move_to(6.5, 6.5)
            cr.line_to(13.5, 13.5)
            cr.move_to(13.5, 6.5)
            cr.line_to(6.5, 13.5)
            cr.stroke()
        return False


class Timeline(Gtk.DrawingArea):
    """The Activity chart: stacked per-VM areas (CPU, memory) or mirrored read/write (disk,
    network), with a hover cursor and tooltip. Data comes from ResourceView's history."""
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.set_size_request(-1, 200)
        self.set_hexpand(True)
        self.mode, self.minutes = 'cpu', 5
        self.hover_x = None
        self.ymax_shown = None
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect('motion-notify-event', lambda _w, e: self._hover(e.x))
        self.connect('leave-notify-event', lambda *_: self._hover(None))
        self.connect('draw', self._draw)

    def _hover(self, x):
        self.hover_x = x
        self.queue_draw()

    def series(self):
        """(layers [(name, rgb, values)], extra, n, kind) for the current mode and range."""
        v = self.view
        n = v.HIST if self.minutes == 15 else v.HIST // 3
        names = [nm for nm in v.names if v.ever_ran(nm, n)]

        def get(name, key, agg='mean'):
            return v.window(v.hist[name][key], n, agg)
        if self.mode == 'cpu':
            return ([(nm, vm_rgb(nm), get(nm, 'cpu')) for nm in names],
                    {'host': v.window(v.hhist['host'], n, 'mean')}, 'pct')
        if self.mode == 'mem':
            return ([(nm, vm_rgb(nm), get(nm, 'rss')) for nm in names],
                    {'other': v.window(v.hhist['other_mem'], n, 'mean')}, 'bytes')
        up, down = ('rd', 'wr') if self.mode == 'disk' else ('rx', 'tx')
        return ([(nm, vm_rgb(nm), get(nm, up, 'max'), get(nm, down, 'max')) for nm in names], {}, 'mirror')

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        px0, px1, py0, py1 = 48, w - 12, 10, h - 22
        pw, ph = px1 - px0, py1 - py0
        layers, extra, kind = self.series()
        view = self.view
        hl = view.hover or view.focus
        n = len(layers[0][2]) if layers else (len(extra.get('host', [])) or 2)
        n = max(2, n)
        # history is pre-filled with zeros; draw only what was really sampled, ending at "now"
        real = min(n, max(2, view.samples // (3 if self.minutes == 15 else 1)))
        skip = n - real
        if skip:
            layers = [(l[0], l[1], *(vals[skip:] for vals in l[2:])) for l in layers]
            extra = {k: v[skip:] for k, v in extra.items()}
        full, n = n, real

        def xat(i):
            return px0 + pw * (i + full - n) / (full - 1)

        axis = (0.48, 0.48, 0.48)

        def label(text, x, y, align='right', color=axis, px=10):
            lay = layout(self, text, px)
            tw, th = lay.get_pixel_size()
            cr.move_to(x - tw if align == 'right' else x - tw / 2 if align == 'center' else x, y - th / 2)
            cr.set_source_rgb(*color)
            PangoCairo.show_layout(cr, lay)

        # x labels
        label(f'−{self.minutes} min', px0, h - 9, 'left')
        label('now', px1, h - 9, 'right')
        label(f'−{self.minutes / 2:g} min', px0 + pw / 2, h - 9, 'center')

        def stacked(ymax, fmt):
            for k in (0.25, 0.5, 0.75, 1):
                y = py1 - ph * k
                cr.set_dash([2, 4])
                cr.move_to(px0, round(y) + 0.5)
                cr.line_to(px1, round(y) + 0.5)
                cr.set_source_rgba(1, 1, 1, 0.05)
                cr.set_line_width(1)
                cr.stroke()
                cr.set_dash([])
                label(fmt(ymax * k), px0 - 8, y)
            cr.rectangle(px0, py1, pw, 1)
            cr.set_source_rgba(1, 1, 1, 0.10)
            cr.fill()
            base = [0.0] * n
            tops = []
            for name, col, vals in layers:
                top = [b + vv for b, vv in zip(base, vals)]
                pts_top = [(xat(i), py1 - ph * min(1.0, t / ymax)) for i, t in enumerate(top)]
                pts_base = [(xat(i), py1 - ph * min(1.0, b / ymax)) for i, b in enumerate(base)]
                dim = hl and hl != name
                a = 0.25 if dim else 1.0
                smooth_path(cr, pts_top)
                smooth_path(cr, pts_base[::-1], move=False)
                cr.close_path()
                g = cairo.LinearGradient(0, py0, 0, py1)
                g.add_color_stop_rgba(0, *col, (0.8 if hl == name else 0.62) * a)
                g.add_color_stop_rgba(1, *col, 0.22 * a)
                cr.set_source(g)
                cr.fill()
                smooth_path(cr, pts_top)
                cr.set_source_rgba(*col, 0.95 * a)
                cr.set_line_width(1.8 if hl == name else 1.2)
                cr.set_line_join(cairo.LINE_JOIN_ROUND)
                cr.stroke()
                tops.append((name, col, vals, pts_top))
                base = top
            return base, tops

        if kind in ('pct', 'bytes'):
            if kind == 'pct':
                host = extra['host'] if extra.get('host') else [0.0] * n
                peak = max(host + [0.0])
                ymax = min(100.0, max(5.0, nice_ceil(peak * 1.15)))
                fmt = lambda v: f'{v:g}%'
            else:
                ymax = float(view.mem_total or 1)
                fmt = lambda v: f'{v / GB:.0f} GB'
            if self.ymax_shown is None or kind != getattr(self, '_kind', None):
                self.ymax_shown = ymax
            self.ymax_shown += (ymax - self.ymax_shown) * 0.5
            self._kind = kind
            ymax = self.ymax_shown
            base, tops = stacked(ymax, fmt)
            if kind == 'pct' and extra.get('host'):
                # everything else on the host: hatched between the lab stack and the host total
                host = extra['host']
                pts_h = [(xat(i), py1 - ph * min(1.0, max(b, v) / ymax)) for i, (b, v) in enumerate(zip(base, host))]
                pts_b = [(xat(i), py1 - ph * min(1.0, b / ymax)) for i, b in enumerate(base)]
                smooth_path(cr, pts_h)
                smooth_path(cr, pts_b[::-1], move=False)
                cr.close_path()
                cr.set_source_rgba(1, 1, 1, 0.04)
                cr.fill_preserve()
                cr.set_source(hatch())
                cr.fill()
                smooth_path(cr, pts_h)
                cr.set_source_rgba(1, 1, 1, 0.85)
                cr.set_line_width(1.5)
                cr.stroke()
                tops.append(('Host total', (1, 1, 1), host, pts_h))
            if kind == 'bytes':
                y = py1 - ph * (1 - view.RESERVE / ymax)
                cr.set_dash([3, 3])
                cr.move_to(px0, round(y) + 0.5)
                cr.line_to(px1, round(y) + 0.5)
                cr.set_source_rgb(*rgb('#FCE100'))
                cr.set_line_width(1.5)
                cr.stroke()
                cr.set_dash([])
                label(f'{fmt_bytes(view.RESERVE, 0)} kept free', px1 - 4, y - 9, 'right', rgb('#FCE100'))
        else:
            mid = py0 + ph / 2
            ups = [sum(l[2][i] for l in layers) for i in range(n)] if layers else [0] * n
            downs = [sum(l[3][i] for l in layers) for i in range(n)] if layers else [0] * n
            top_u, top_d = max(65536.0, nice_ceil(max(ups) * 1.1)), max(65536.0, nice_ceil(max(downs) * 1.1))
            cr.rectangle(px0, round(mid) - 0.5, pw, 1)
            cr.set_source_rgba(1, 1, 1, 0.18)
            cr.fill()
            label(fmt_rate(top_u), px0 - 8, py0 + 4)
            label(fmt_rate(top_d), px0 - 8, py1 - 4)
            label('0', px0 - 8, mid)
            tops = []
            for half, top_v, sign, idx in (('up', top_u, -1, 2), ('down', top_d, 1, 3)):
                base = [0.0] * n
                for layer in layers:
                    name, col, vals = layer[0], layer[1], layer[idx]
                    top = [b + vv for b, vv in zip(base, vals)]
                    pts_t = [(xat(i), mid + sign * (ph / 2 - 2) * min(1.0, t / top_v)) for i, t in enumerate(top)]
                    pts_b = [(xat(i), mid + sign * (ph / 2 - 2) * min(1.0, b / top_v)) for i, b in enumerate(base)]
                    a = 0.25 if hl and hl != name else 1.0
                    smooth_path(cr, pts_t)
                    smooth_path(cr, pts_b[::-1], move=False)
                    cr.close_path()
                    g = cairo.LinearGradient(0, mid, 0, mid + sign * ph / 2)
                    g.add_color_stop_rgba(0, *col, 0.08 * a)
                    g.add_color_stop_rgba(1, *col, 0.55 * a)
                    cr.set_source(g)
                    cr.fill()
                    smooth_path(cr, pts_t)
                    cr.set_source_rgba(*col, 0.9 * a)
                    cr.set_line_width(1.8 if hl == name else 1.1)
                    cr.stroke()
                    if half == 'up':
                        tops.append((name, col, [u + d for u, d in zip(layer[2], layer[3])], pts_t))
                    base = top
            cap = 'Read ▲  Write ▼' if self.mode == 'disk' else 'Received ▲  Sent ▼'
            label(cap, px1 - 4, py0 + 6, 'right')
            # peak marker
            best = max(((v, i, l[0], l[1]) for l in layers for i, v in enumerate(l[2])), default=None)
            if best and best[0] > 1024:
                v, i, name, col = best
                x = xat(i)
                y = mid - (ph / 2 - 2) * min(1.0, v / top_u)
                cr.arc(x, y, 3, 0, 2 * math.pi)
                cr.set_source_rgb(*col)
                cr.fill()
                label(f'{fmt_rate(v)} · {short(name)}', min(px1 - 4, x + 6), max(py0 + 8, y - 8), 'left' if x < px0 + pw * 0.7 else 'right', col)

        # left-edge fade into the card
        g = cairo.LinearGradient(px0, 0, px0 + 40, 0)
        g.add_color_stop_rgba(0, 0.169, 0.169, 0.169, 1)
        g.add_color_stop_rgba(1, 0.169, 0.169, 0.169, 0)
        cr.rectangle(px0, py0 - 4, 40, ph + 6)
        cr.set_source(g)
        cr.fill()
        # "now" dot on the top layer
        if tops:
            x, y = tops[-1][3][-1]
            col = tops[-1][1]
            cr.arc(x, y, 7, 0, 2 * math.pi)
            cr.set_source_rgba(*col, 0.2)
            cr.fill()
            cr.arc(x, y, 3, 0, 2 * math.pi)
            cr.set_source_rgba(*col, 1)
            cr.fill()
        if self.hover_x is not None and px0 <= self.hover_x <= px1 and tops:
            self._tooltip(cr, tops, n, full, xat, px0, pw, py0, py1, w, kind)
        return False

    def _tooltip(self, cr, tops, n, full, xat, px0, pw, py0, py1, w, kind):
        i = max(0, min(n - 1, round((self.hover_x - px0) / pw * (full - 1)) - (full - n)))
        x = xat(i)
        cr.rectangle(round(x) - 0.5, py0, 1, py1 - py0)
        cr.set_source_rgba(1, 1, 1, 0.28)
        cr.fill()
        rows = []
        for name, col, vals, pts in tops:
            cr.arc(pts[i][0], pts[i][1], 3.5, 0, 2 * math.pi)
            cr.set_source_rgb(*col)
            cr.fill_preserve()
            cr.set_source_rgb(0.169, 0.169, 0.169)
            cr.set_line_width(1.5)
            cr.stroke()
            v = vals[i]
            text = f'{v:.1f}%' if kind == 'pct' else fmt_bytes(v) if kind == 'bytes' else fmt_rate(v)
            rows.append((short(name), col, text, v))
        rows = sorted(rows, key=lambda r: -r[3])[:7]
        step = self.view.HIST_STEP * (3 if self.minutes == 15 else 1)
        ago = (n - 1 - i) * step
        head = 'now' if ago < 1 else f'−{int(ago // 60)} m {int(ago % 60):02d} s'
        lays = [(layout(self, nm, 11), col, layout(self, t, 11, bold=True)) for nm, col, t, _v in rows]
        head_l = layout(self, head, 10)
        bw = max([head_l.get_pixel_size()[0]] + [a.get_pixel_size()[0] + b.get_pixel_size()[0] + 36 for a, _c, b in lays]) + 20
        bh = 26 + 17 * len(lays)
        bx = x + 12 if x + 12 + bw < w - 4 else x - 12 - bw
        by = py0 + 4
        rrect(cr, bx + 2, by + 4, bw, bh, 8)
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.fill()
        rrect(cr, bx, by, bw, bh, 8)
        cr.set_source_rgba(0.173, 0.173, 0.173, 0.96)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.10)
        cr.set_line_width(1)
        cr.stroke()
        cr.move_to(bx + 10, by + 7)
        cr.set_source_rgb(0.6, 0.6, 0.6)
        PangoCairo.show_layout(cr, head_l)
        for k, (nl, col, vl) in enumerate(lays):
            y = by + 25 + 17 * k
            cr.arc(bx + 13, y + 7, 3, 0, 2 * math.pi)
            cr.set_source_rgb(*col)
            cr.fill()
            cr.move_to(bx + 22, y)
            cr.set_source_rgb(0.85, 0.85, 0.85)
            PangoCairo.show_layout(cr, nl)
            cr.move_to(bx + bw - 10 - vl.get_pixel_size()[0], y)
            cr.set_source_rgb(1, 1, 1)
            PangoCairo.show_layout(cr, vl)


class RamBar(Gtk.DrawingArea):
    """Where host RAM goes: each running VM, QEMU overhead, Linux & apps, free; the reserve
    kept for the host; and dashed "ghosts" for frozen VMs (what Resume would need)."""
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.set_size_request(-1, 50)
        self.set_hexpand(True)
        self.set_has_tooltip(True)
        self.data = None
        self.hits = []
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect('motion-notify-event', lambda _w, e: self._hover(e.x))
        self.connect('leave-notify-event', lambda *_: self._hover(None))
        self.connect('query-tooltip', self._tip)
        self.connect('draw', self._draw)

    def set(self, data):
        self.data = data
        self.queue_draw()

    def _hit(self, x):
        for x0, x1, name, _text in self.hits:
            if x0 <= x <= x1:
                return name
        return None

    def _hover(self, x):
        name = self._hit(x) if x is not None else None
        name = name if name in self.view.names else None
        if name != self.view.hover:
            self.view.set_hover(name)

    def _tip(self, _w, x, _y, _kb, tip):
        for x0, x1, _name, text in self.hits:
            if x0 <= x <= x1:
                tip.set_text(text)
                return True
        return False

    def _draw(self, _w, cr):
        if not self.data:
            return False
        d = self.data
        w = self.get_allocated_width()
        bh = 30
        total = d['total']
        sx = w / total
        hl = self.view.hover or self.view.focus
        rrect(cr, 0, 0, w, bh, 7)
        cr.save()
        cr.clip_preserve()
        cr.set_source_rgba(1, 1, 1, 0.05)
        cr.fill()
        x = 0.0
        self.hits = []
        segs = [(name, vm_rgb(name), b, f'{short(name)}: {fmt_bytes(b)} in host RAM') for name, b in d['vms']]
        for name, col, b, tip in segs:
            sw = b * sx
            g = cairo.LinearGradient(0, 0, 0, bh)
            a = 0.4 if hl and hl != name else 1.0
            g.add_color_stop_rgba(0, *col, a)
            g.add_color_stop_rgba(1, *col, 0.78 * a)
            cr.rectangle(x, 0, sw, bh)
            cr.set_source(g)
            cr.fill()
            self.hits.append((x, x + sw, name, tip))
            x += sw
            cr.rectangle(x - 0.75, 0, 1.5, bh)
            cr.set_source_rgb(0.169, 0.169, 0.169)
            cr.fill()
        ov = d['overhead'] * sx
        if ov > 0.5:
            cr.rectangle(x, 0, ov, bh)
            cr.set_source_rgba(1, 1, 1, 0.10)
            cr.fill_preserve()
            cr.set_source(hatch())
            cr.fill()
            self.hits.append((x, x + ov, 'qemu', f'QEMU overhead: {fmt_bytes(d["overhead"])}'))
            x += ov
        oth = d['other'] * sx
        cr.rectangle(x, 0, oth, bh)
        cr.set_source_rgba(*rgb('#5A5A5A'), 0.4 if hl else 1)
        cr.fill()
        self.hits.append((x, x + oth, 'other', f'Linux & apps: {fmt_bytes(d["other"])}'))
        x += oth
        used_end = x
        free_hit = (x, w, 'free', f'Free: {fmt_bytes(d["avail"])}')
        cr.rectangle(0, 0.5, w, 1)
        cr.set_source_rgba(1, 1, 1, 0.12)
        cr.fill()
        # reserve zone
        rx = (total - d['reserve']) * sx
        cr.rectangle(rx, 0, w - rx, bh)
        cr.set_source_rgba(0.988, 0.882, 0, 0.08)
        cr.fill()
        cr.restore()
        cr.set_dash([3, 3])
        cr.move_to(round(rx) + 0.5, -2)
        cr.line_to(round(rx) + 0.5, bh + 2)
        cr.set_source_rgb(*rgb('#FCE100'))
        cr.set_line_width(1.5)
        cr.stroke()
        # frozen ghosts
        gx = used_end + 3
        for name, b in d['frozen']:
            gw = b * sx - 4
            if gw < 6:
                continue
            if gx + gw > rx:
                over_x = max(gx, rx)
                cr.rectangle(over_x, 4, gx + gw - over_x, bh - 8)
                cr.set_source_rgba(1, 0.6, 0.643, 0.25)
                cr.fill()
            cr.set_dash([4, 3])
            rrect(cr, gx, 3.5, gw, bh - 7, 4)
            cr.set_source_rgb(*rgb('#C3B1FF'))
            cr.set_line_width(1.2)
            cr.stroke()
            cr.set_dash([])
            lay = layout(self, f'❄ {short(name)}', 10)
            tw, th = lay.get_pixel_size()
            if tw < gw - 6:
                cr.move_to(gx + (gw - tw) / 2, (bh - th) / 2)
                PangoCairo.show_layout(cr, lay)
            self.hits.append((gx, gx + gw, name, f'{short(name)} is frozen: resuming needs {fmt_bytes(b)}'))
            gx += gw + 4
        self.hits.append(free_hit)  # after the ghosts, so hovering a ghost finds the ghost
        cr.set_dash([])
        # label lane
        x = 0.0
        for name, col, b, _tip in segs:
            sw = b * sx
            lay = layout(self, f'{short(name)} {fmt_bytes(b, 0)}', 10)
            tw, th = lay.get_pixel_size()
            if sw >= tw + 6:
                cr.move_to(x + 2, bh + 4)
                cr.set_source_rgb(*col)
                PangoCairo.show_layout(cr, lay)
            x += sw
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
            if re.match(r'\d{4}-', parts[-4]):
                rows.append((' '.join(parts[1:-6]), f'{parts[-4]} {parts[-3]}'))
            else:
                rows.append((parts[1], ''))
    return rows


class GuestProbe:
    """Background details that are too slow for the 2 s tick: Windows info, real IPs, signed-in
    users, C: usage and clock drift through the guest agent (each running VM with tools, every
    60 s), and the checkpoint list (every 5 min). One job at a time on its own thread and libvirt
    connection; a job that hangs is left to finish on its own and the VM is skipped meanwhile.
    Results come back to the GTK thread through app.on_probe(name, kind, data)."""
    GUEST_EVERY, CKPT_EVERY, BACKOFF = 60, 300, 300

    def __init__(self, app):
        self.app = app
        self.wanted = {}  # name -> (running, agent, up_since); published by the GTK thread every tick
        self.due = {}  # (kind, name) -> monotonic time
        self.fails = {}
        self.in_flight = set()
        self.poked = set()  # (kind, name) wanted as soon as possible
        self.logged = set()
        self._stop = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    def publish(self, wanted):
        now = time.monotonic()
        for name, (running, agent, _up) in wanted.items():
            old = self.wanted.get(name)
            if running and agent and not (old and old[0] and old[1]):
                self.due[('guest', name)] = now + 15  # agent just came up: ask soon
        self.wanted = wanted

    def poke(self, name, kind='ckpt'):
        self.poked.add((kind, name))

    def stop(self):
        self._stop.set()

    def _next_job(self):
        now, wall = time.monotonic(), time.time()
        if self.app.lab_run:
            return None
        for i, (name, (running, agent, up)) in enumerate(list(self.wanted.items())):
            if name in self.in_flight:
                continue
            if ('ckpt', name) in self.poked or self.due.setdefault(('ckpt', name), now + 2 * i) <= now:
                self.poked.discard(('ckpt', name))
                return 'ckpt', name
            if running and agent and (up is None or wall - up >= 90):
                if ('guest', name) in self.poked or self.due.setdefault(('guest', name), now) <= now:
                    self.poked.discard(('guest', name))
                    return 'guest', name
        return None

    def _worker(self):
        conn = None
        while not self._stop.wait(2):
            job = self._next_job()
            if not job:
                continue
            try:
                conn = conn or libvirt.open(URI)
            except libvirt.libvirtError:
                conn = None
                continue
            kind, name = job
            self.in_flight.add(name)
            helper = threading.Thread(target=self._run, args=(conn, kind, name), daemon=True)
            helper.start()
            helper.join(10)  # a slow agent keeps going on its own; _run does the bookkeeping when it ends
            if helper.is_alive():
                self.due[job] = time.monotonic() + self.BACKOFF  # don't pick this VM again meanwhile

    def _run(self, conn, kind, name):
        box = {}
        try:
            dom = conn.lookupByName(name)
            if kind == 'ckpt':
                data = list_checkpoints(dom)
            else:
                types = (libvirt.VIR_DOMAIN_GUEST_INFO_OS | libvirt.VIR_DOMAIN_GUEST_INFO_HOSTNAME
                         | libvirt.VIR_DOMAIN_GUEST_INFO_USERS | libvirt.VIR_DOMAIN_GUEST_INFO_FILESYSTEM)
                data = {'info': dom.guestInfo(types, 0), 'at': time.time()}
                if name not in self.logged:
                    self.logged.add(name)
                try:
                    data['ifs'] = dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT, 0)
                except libvirt.libvirtError:
                    data['ifs'] = None
                try:
                    t = dom.getTime(0)
                    data['drift'] = t['seconds'] + t.get('nseconds', 0) / 1e9 - time.time()
                except libvirt.libvirtError:
                    data['drift'] = None
            box['data'] = data
            GLib.idle_add(self.app.on_probe, name, kind, data, None)
        except Exception as e:  # any failure counts against this job, never kills the probe
            box['err'] = e
            GLib.idle_add(self.app.on_probe, name, kind, None, e)
        finally:
            job = (kind, name)
            every = self.GUEST_EVERY if kind == 'guest' else self.CKPT_EVERY
            if 'err' in box:
                self.fails[job] = self.fails.get(job, 0) + 1
                every = self.BACKOFF if self.fails[job] >= 2 else every
            else:
                self.fails[job] = 0
            self.due[job] = time.monotonic() + every  # pokes live in self.poked, so none is lost here
            self.in_flight.discard(name)


def mini_stat(caption, value_widget, visual=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    cap = Gtk.Label(label=caption.upper(), xalign=0)
    cap.get_style_context().add_class('mini-cap')
    box.pack_start(cap, False, False, 0)
    box.pack_start(value_widget, False, False, 0)
    if visual is not None:
        box.pack_start(visual, False, False, 0)
    return box


def styled(widget, *classes):
    ctx = widget.get_style_context()
    for c in classes:
        ctx.add_class(c)
    return widget


def set_classes(widget, wanted, among):
    ctx = widget.get_style_context()
    for c in among:
        if c in wanted and not ctx.has_class(c):
            ctx.add_class(c)
        elif c not in wanted and ctx.has_class(c):
            ctx.remove_class(c)


def set_text(label, text, markup=False):
    if (label.get_label() if markup else label.get_text()) != text:
        label.set_markup(text) if markup else label.set_text(text)


class VmRow(Gtk.EventBox):
    """A VM card in the List: identity, live CPU / memory / I/O (or a summary when it's off),
    power buttons, a facts strip (addresses, signed in, uptime, C:, checkpoints), warning chips,
    and an expandable detail panel."""
    def __init__(self, app, dom):
        super().__init__(visible_window=False)  # a window of its own, so clicks on the card reach it
        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(self.card)
        self.app, self.dom, self.name = app, dom, dom.name()
        kind, role, addr = LAB.get(self.name, ('client', '', ''))
        self.kind, self.role, self.plan_addr = kind, role, addr
        styled(self.card, 'card', 'vm')
        self.state = None
        self.key = None  # state, or 'saved' when it's off with a saved memory image
        self.agent = None
        self.facts = {}
        self.guest = None  # last GuestProbe result
        self.drifts = []
        self.checkpoints = None
        self.up_since = None
        self.last_used = None
        self.saved_img = None
        self.disk_used = 0
        self.capacity = None  # virtual disk size
        self._xml_tick = 0

        # Row A
        top = Gtk.Box(spacing=12)
        self.card.pack_start(top, False, False, 0)
        self.icon = Gtk.Image.new_from_pixbuf(tile_pixbuf(kind, False))
        self.icon.set_valign(Gtk.Align.CENTER)
        top.pack_start(self.icon, False, False, 0)
        ident = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, valign=Gtk.Align.CENTER)
        line1 = Gtk.Box(spacing=8)
        line1.pack_start(styled(Gtk.Label(label=short(self.name), xalign=0), 'vm-name'), False, False, 0)
        self.pill = styled(Gtk.Label(valign=Gtk.Align.CENTER), 'pill')
        line1.pack_start(self.pill, False, False, 0)
        self.os_chip = styled(Gtk.Label(valign=Gtk.Align.CENTER, no_show_all=True), 'os-chip')
        line1.pack_start(self.os_chip, False, False, 0)
        ident.pack_start(line1, False, False, 0)
        ident.pack_start(styled(Gtk.Label(label=role, xalign=0, ellipsize=Pango.EllipsizeMode.END), 'vm-role'),
                         False, False, 0)
        top.pack_start(ident, True, True, 0)

        self.metrics = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150,
                                 valign=Gtk.Align.CENTER)
        self.metrics.set_size_request(312, -1)
        live = Gtk.Box(spacing=12, homogeneous=True)
        self.spark = Sparkline(w=96, h=18, n=48)
        self.cpu_val = styled(Gtk.Label(xalign=0), 'mini-val')
        live.pack_start(mini_stat('CPU', self.cpu_val, self.spark), True, True, 0)
        self.mem_bar = Meter(4, 96)
        self.mem_val = styled(Gtk.Label(xalign=0), 'mini-val')
        live.pack_start(mini_stat('Memory', self.mem_val, self.mem_bar), True, True, 0)
        io = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.net_val = styled(Gtk.Label(xalign=0), 'stat')
        self.disk_val = styled(Gtk.Label(xalign=0), 'stat')
        io.pack_start(self.net_val, False, False, 0)
        io.pack_start(self.disk_val, False, False, 0)
        live.pack_start(mini_stat('I/O', io), True, True, 0)
        self.metrics.add_named(live, 'live')
        off = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        self.off_sum = styled(Gtk.Label(xalign=1), 'off-sum')
        self.off_sub = styled(Gtk.Label(xalign=1), 'off-sub')
        off.pack_start(self.off_sum, False, False, 0)
        off.pack_start(self.off_sub, False, False, 0)
        self.metrics.add_named(off, 'off')
        top.pack_start(self.metrics, False, False, 6)

        self.power = icon_button('media-playback-start-symbolic', 'Start', self.toggle_power)
        self.connect_btn = text_button('Connect', 'subtle', lambda: app.open_console(self.name))
        self.more = icon_button('view-more-symbolic', 'More')
        self.more.connect('clicked', lambda *_: self.show_menu())
        self.chevron = icon_button('pan-down-symbolic', 'Details', lambda: self.set_expanded(not self.rev.get_reveal_child()))
        styled(self.chevron, 'chevron')
        for wdg in (self.power, self.connect_btn, self.more, self.chevron):
            top.pack_start(wdg, False, False, 0)

        # Facts strip
        self.facts_box = styled(Gtk.Box(spacing=0), 'facts')
        self.card.pack_start(self.facts_box, False, False, 0)
        self.f_addr = self._fact('Address', hexpand=True, markup=True)
        self.f_user = self._fact('Signed in', 140)
        self.f_up = self._fact('Uptime', 96)
        cbox = Gtk.Box(spacing=8)
        self.c_bar = Meter(4, 56)
        self.c_val = styled(Gtk.Label(xalign=0), 'fact-val')
        cbox.pack_start(self.c_bar, False, False, 0)
        cbox.pack_start(self.c_val, False, False, 0)
        self.f_c = self._fact('C: drive', 156, widget=cbox)
        self.f_ck = self._fact('Checkpoints', 104, last=True)
        self.pxe_note = styled(Gtk.Label(xalign=0, no_show_all=True), 'fact-val', 'muted')
        self.pxe_note.set_margin_start(52)
        self.pxe_note.set_margin_top(8)
        self.card.pack_start(self.pxe_note, False, False, 0)

        # Warning chips
        self.chips = styled(Gtk.Box(spacing=6, no_show_all=True), 'chips')
        self.card.pack_start(self.chips, False, False, 0)
        self.chip_widgets = {}

        # Detail panel
        self.rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, transition_duration=180)
        self.card.pack_start(self.rev, False, False, 0)
        detail = styled(Gtk.Box(spacing=24, homogeneous=True), 'vm-detail')
        self.rev.add(detail)
        self.d_sys = self._detail_col(detail, 'System')
        self.d_net = self._detail_col(detail, 'Network')
        self.d_sto = self._detail_col(detail, 'Storage')
        self.rev.connect('notify::child-revealed', lambda *_: self.fill_detail())

        self.press = Gtk.GestureMultiPress.new(self)  # double-click on the card toggles the details
        self.press.connect('pressed', lambda _g, n, _x, _y: n == 2 and self.set_expanded(not self.rev.get_reveal_child()))
        if self.name in load_settings().get('expanded', []):
            self.rev.set_reveal_child(True)
            set_icon(self.chevron, 'pan-up-symbolic')
        self.refresh()

    # -- building blocks
    def _fact(self, caption, width=-1, hexpand=False, markup=False, widget=None, last=False):
        cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=hexpand)
        cell.set_size_request(width, -1)
        cell.pack_start(styled(Gtk.Label(label=caption.upper(), xalign=0), 'fact-cap'), False, False, 0)
        val = widget or styled(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, use_markup=markup), 'fact-val')
        cell.pack_start(val, False, False, 0)
        self.facts_box.pack_start(cell, hexpand, hexpand, 0)
        if not last:
            self.facts_box.pack_start(styled(Gtk.Box(), 'fact-sep'), False, False, 0)
        return val

    def _detail_col(self, parent, title):
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        col.pack_start(styled(Gtk.Label(label=title.upper(), xalign=0), 'detail-head'), False, False, 0)
        grid = Gtk.Grid(row_spacing=4, column_spacing=12)
        col.pack_start(grid, False, False, 0)
        parent.pack_start(col, True, True, 0)
        grid.rows = []
        return grid

    def _set_detail(self, grid, rows):
        """rows: [(key, value, cls)] — rebuilds only when the keys change."""
        keys = [r[0] for r in rows]
        if keys != [r[0] for r in grid.rows]:
            for child in grid.get_children():
                grid.remove(child)
            grid.rows = []
            for i, (k, _v, _c) in enumerate(rows):
                kl = styled(Gtk.Label(label=k, xalign=0, yalign=0, width_chars=10), 'detail-key')
                vl = styled(Gtk.Label(xalign=0, selectable=True, wrap=True, max_width_chars=34), 'detail-val')
                vl.set_can_focus(False)
                grid.attach(kl, 0, i, 1, 1)
                grid.attach(vl, 1, i, 1, 1)
                grid.rows.append((k, vl))
            grid.show_all()
        for (k, v, cls), (_k, vl) in zip(rows, grid.rows):
            set_text(vl, v)
            set_classes(vl, {cls} if cls else set(), ('bad', 'muted'))

    def set_expanded(self, on):
        self.rev.set_reveal_child(on)
        set_icon(self.chevron, 'pan-up-symbolic' if on else 'pan-down-symbolic')
        data = load_settings()
        names = set(data.get('expanded', []))
        (names.add if on else names.discard)(self.name)
        data['expanded'] = sorted(names)
        save_settings(data)
        if on:
            self.fill_detail()

    # -- data
    def refresh_tools(self):
        """Static facts and the guest-agent state from the live XML (every ~10 s)."""
        try:
            self.facts = parse_facts(self.dom.XMLDesc(0))
        except (libvirt.libvirtError, ET.ParseError):
            return
        self.agent = self.facts['agent']
        path = self.facts.get('disk')
        if path:
            self.capacity = qcow2_virtual_size(path) or self.capacity
            try:
                st = os.stat(path)
                self.disk_used = st.st_blocks * 512
                if self.state == libvirt.VIR_DOMAIN_SHUTOFF:
                    self.last_used = st.st_mtime
            except OSError:
                pass
        self.saved_img = save_image(self.name) if self.key == 'saved' else None

    @property
    def capacity_text(self):
        return fmt_bytes(self.capacity, 0) if self.capacity else '?'

    @property
    def empty_disk(self):
        return self.facts.get('disk') is not None and 0 < self.disk_used < (1 << 20)

    def refresh(self):
        try:
            state, _max, mem, vcpus, _cpu_time = self.dom.info()
        except libvirt.libvirtError:
            return
        saved = state == libvirt.VIR_DOMAIN_SHUTOFF and self.dom.hasManagedSaveImage(0)
        key = 'saved' if saved else state
        text, cls = STATE_TEXT.get(key, ('Unknown', 'off'))
        if key != self.key:
            self.key = key
            set_classes(self.pill, {cls}, ('running', 'off', 'busy', 'paused', 'error', 'saved'))
            self.pill.set_text(text)
            self.icon.set_from_pixbuf(tile_pixbuf(self.kind, key == libvirt.VIR_DOMAIN_RUNNING, frozen=key == 'saved'))
            self.icon.set_tooltip_text('Frozen: memory saved to disk; Resume continues right here' if key == 'saved' else None)
            self._xml_tick = 0
        if state != self.state:
            running = state == libvirt.VIR_DOMAIN_RUNNING
            set_icon(self.power, 'system-shutdown-symbolic' if running else 'media-playback-start-symbolic')
            self.power.set_tooltip_text('Power options' if running else 'Start')
            self.state = state
            self.up_since = None
            if not running:
                self.spark.clear()
                self.guest = None
                self.drifts = []
            self._xml_tick = 0
        self._xml_tick -= 1
        if self._xml_tick <= 0:  # every ~10 s, and right after a state change
            self.refresh_tools()
            self._xml_tick = 5
        live = state in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_PAUSED)
        self.refresh_os_chip(live)
        if live and self.up_since is None:
            self.up_since = qemu_started(self.name)
        self.metrics.set_visible_child_name('live' if live else 'off')
        set_classes(self.card, set() if live else {'off'}, ('off',))
        assigned = self.facts.get('mem') or mem * 1024
        vcpus = self.facts.get('vcpus') or vcpus
        if live:
            r = self.app.resources.rates.get(self.name) if getattr(self.app, 'resources', None) else None
            if r and 'cpu_pct' in r:
                self.spark.push(r['cpu_pct'])
                set_text(self.cpu_val, f'{r["cpu_pct"]:.0f}%')
                set_text(self.mem_val, f'{r["rss"] / GB:.1f} / {assigned / GB:g} GB')
                scale = assigned * 1.15
                self.mem_bar.set([(min(r['rss'], assigned) / scale, LAB_RGBA),
                                  (max(0, r['rss'] - assigned) / scale, (1, 1, 1, 0.35))])
                set_text(self.net_val, f'↓↑ {fmt_rate(r["rx"] + r["tx"])}')
                set_text(self.disk_val, f'R/W {fmt_rate(r["rd"] + r["wr"])}')
            else:
                paused = state == libvirt.VIR_DOMAIN_PAUSED
                set_text(self.cpu_val, 'Paused' if paused else '…')
                set_text(self.mem_val, f'{r["rss"] / GB:.1f} / {assigned / GB:g} GB' if r and 'rss' in r else f'{assigned / GB:g} GB')
                set_text(self.net_val, '↓↑ —')
                set_text(self.disk_val, 'R/W —')
                if paused:
                    self.spark.clear()
        else:
            if self.empty_disk:
                set_text(self.off_sum, 'Empty disk · PXE / OSD target')
                set_text(self.off_sub, f'{vcpus} vCPU · {assigned / GB:g} GB · {self.capacity_text} virtual')
            elif key == 'saved':
                img = self.saved_img
                set_text(self.off_sum, f'Frozen {fmt_ago(img[1])}' if img else 'Frozen')
                set_text(self.off_sub, (f'{fmt_bytes(img[0])} memory image' if img else
                                        f'up to {assigned / GB:g} GB memory image') + ' · Resume continues here')
            else:
                set_text(self.off_sum, f'Last used {fmt_ago(self.last_used)}' if self.last_used else text)
                set_text(self.off_sub, f'{vcpus} vCPU · {assigned / GB:g} GB · {fmt_bytes(self.disk_used)} on disk')
        self.refresh_facts(live)
        self.refresh_chips(live)
        if self.rev.get_child_revealed():
            self.fill_detail()

    def addresses(self):
        """[(network, ip, source)] per NIC: agent IPs when we have them, else the lab plan."""
        nics = self.facts.get('nics', [])
        out = []
        ifs = (self.guest or {}).get('ifs') or {}
        by_mac = {}
        for v in ifs.values():
            ips = [a['addr'] for a in (v.get('addrs') or []) if a.get('type') == libvirt.VIR_IP_ADDR_TYPE_IPV4
                   and not a['addr'].startswith(('127.', '169.254.'))]
            if v.get('hwaddr') and ips:
                by_mac[v['hwaddr'].lower()] = ips[0]
        plan = [a.strip() for a in self.plan_addr.split('·')] if self.plan_addr and self.plan_addr != 'DHCP' else []
        for i, nic in enumerate(nics):
            ip = by_mac.get(nic['mac'].lower())
            if ip:
                out.append((nic['network'], ip, 'guest agent'))
            elif i < len(plan):
                out.append((nic['network'], plan[i], 'static (lab plan)'))
            else:
                out.append((nic['network'], None, 'DHCP'))
        return out

    def refresh_facts(self, live):
        pxe = self.empty_disk and not live
        self.facts_box.set_visible(not pxe)
        self.pxe_note.set_visible(pxe)
        if pxe:
            set_text(self.pxe_note, f'PXE target · empty {self.capacity_text} disk · boots from the network')
            return
        parts, tips = [], []
        for net, ip, src in self.addresses():
            dot = NET_KIND.get(net, ('', '', '#9a9a9a'))[2]
            parts.append(f'<span foreground="{dot}">●</span> {ip}' if ip else
                         f'<span foreground="{dot}">●</span> <span foreground="#7e7e7e"><i>DHCP'
                         + (' · needs guest tools' if live and not self.agent else '') + '</i></span>')
            tips.append(f'{net}: {ip or "DHCP"} ({src})')
        for nic, tip in zip(self.facts.get('nics', []), list(tips)):
            tips[tips.index(tip)] = f'{tip}\n    MAC {nic["mac"]} · {nic["vnet"] or "—"} · {nic["model"]}'
        set_text(self.f_addr, '   '.join(parts) or '<span foreground="#7e7e7e">—</span>', markup=True)
        self.f_addr.set_tooltip_text('\n'.join(tips) or None)
        info = (self.guest or {}).get('info') or {}
        if live and self.guest:
            users = [f'{info.get(f"user.{i}.domain", "")}\\{info.get(f"user.{i}.name", "")}'.lstrip('\\')
                     for i in range(int(info.get('user.count', 0) or 0))]
            set_text(self.f_user, (users[0] + (f'  +{len(users) - 1}' if len(users) > 1 else '')) if users else 'Nobody')
            set_classes(self.f_user, set() if users else {'muted'}, ('muted',))
        else:
            set_text(self.f_user, '—')
            set_classes(self.f_user, {'muted'}, ('muted',))
        if live:
            set_text(self.f_up, fmt_span(time.time() - self.up_since) if self.up_since else '…')
            self.f_up.set_tooltip_text(time.strftime('QEMU started %a %H:%M. ', time.localtime(self.up_since))
                                       + "A restart inside Windows doesn't reset this." if self.up_since else None)
        else:
            set_text(self.f_up, 'Frozen' if self.key == 'saved' else 'Off')
        set_classes(self.f_up, set() if live else {'muted'}, ('muted',))
        c = self.c_drive()
        if c and live:
            used, total = c
            frac = used / total if total else 0
            col = (*rgb('#FF99A4'), 1) if frac >= 0.95 else (*rgb('#FCE100'), 1) if frac >= 0.85 else LAB_RGBA
            self.c_bar.set([(frac, col)])
            self.c_bar.show()
            set_text(self.c_val, f'{used / GB:.0f} / {total / GB:.0f} GB')
            set_classes(self.c_val, set(), ('muted',))
            self.c_val.set_tooltip_text(f'Inside Windows: {fmt_bytes(total - used)} free')
        else:
            self.c_bar.hide()
            set_text(self.c_val, f'{fmt_bytes(self.disk_used)} on host' if self.disk_used else '—')
            set_classes(self.c_val, {'muted'}, ('muted',))
            self.c_val.set_tooltip_text('Size of the disk image on the host. What Windows sees needs the guest tools.')
        if self.checkpoints is None:
            set_text(self.f_ck, '…')
        elif not self.checkpoints:
            set_text(self.f_ck, 'None')
        else:
            newest = self.checkpoints[-1][1]
            date = time.strftime('%b %-d', time.strptime(newest[:10], '%Y-%m-%d')) if newest[:4].isdigit() else ''
            set_text(self.f_ck, f'{len(self.checkpoints)}' + (f' · {date}' if date else ''))
        set_classes(self.f_ck, {'muted'} if not self.checkpoints else set(), ('muted',))

    def c_drive(self):
        info = (self.guest or {}).get('info') or {}
        for i in range(int(info.get('fs.count', 0) or 0)):
            if info.get(f'fs.{i}.mountpoint', '').upper().startswith('C:'):
                try:
                    return int(info[f'fs.{i}.used-bytes']), int(info[f'fs.{i}.total-bytes'])
                except (KeyError, ValueError):
                    return None
        return None

    def warnings(self, live=None):
        """[(sev, chip text, tooltip, action)] for the chips row and the Resources insights."""
        live = self.running if live is None else live
        out = []
        if self.key == libvirt.VIR_DOMAIN_CRASHED:
            out.append(('crit', 'Crashed — use Power off, then Start', 'Windows stopped responding to the VM.', None))
        if not live:
            return out
        if len(self.drifts) >= 2 and all(abs(d) > 60 for d in self.drifts[-2:]):
            d = self.drifts[-1]
            out.append(('crit' if abs(d) > 240 else 'warn', f'Clock {fmt_drift(d)} {"behind" if d < 0 else "ahead"}',
                        "Kerberos sign-in fails beyond 5 minutes. Click to set the VM's clock from the host.",
                        'sync'))
        c = self.c_drive()
        if c and c[1] and c[0] / c[1] >= 0.90:
            out.append(('warn', f'C: {fmt_bytes(c[1] - c[0], 0)} free', 'The Windows system drive is almost full.', None))
        if (self.agent and self.up_since and time.time() - self.up_since > 180
                and self.app.probe.fails.get(('guest', self.name), 0) >= 2):
            out.append(('warn', 'Guest tools not answering', 'The QEMU guest agent is installed but not replying.', None))
        return out

    def refresh_chips(self, live):
        warns = self.warnings(live)
        keys = [w[1] for w in warns]
        if keys != list(self.chip_widgets):
            for child in self.chips.get_children():
                self.chips.remove(child)
            self.chip_widgets = {}
            for sev, text, tip, action in warns:
                if action == 'sync':
                    chip = Gtk.Button(label=text)
                    chip.connect('clicked', lambda *_: self.sync_clock())
                else:
                    chip = Gtk.Label(label=text)
                styled(chip, 'chip', 'error' if sev == 'crit' else 'warn')
                chip.set_tooltip_text(tip)
                chip.set_valign(Gtk.Align.CENTER)
                self.chips.pack_start(chip, False, False, 0)
                self.chip_widgets[text] = chip
                chip.show()
            self.chips.set_visible(bool(warns))

    def sync_clock(self):
        name = short(self.name)
        self.app.toast.show(f'Setting {name}\'s clock from the host…', 3)

        def done(ok, err):
            self.app.toast.show(f'{name}: {err}' if err else f'{name}: clock set' if ok else
                                f'{name}: the guest agent didn\'t answer', 4)
            self.drifts = []
            GLib.timeout_add_seconds(15, lambda: (self.app.probe.poke(self.name, 'guest'), False)[1])
        run_bg(lambda: LabManager.sync_clock(self.dom), done)

    def apply_guest(self, data):
        self.guest = data
        if data.get('drift') is not None:
            self.drifts = (self.drifts + [data['drift']])[-3:]
        info = data.get('info') or {}
        label = os_label(info)
        if label:
            self.os_chip.set_text(label)
            set_classes(self.os_chip, set(), ('none',))
            host = info.get('hostname', '')
            self.os_chip.set_tooltip_text(f'{info.get("os.pretty-name", "")}\nHost name {host}' if host else
                                          info.get('os.pretty-name', ''))
            self.os_chip.show()
        self.refresh_facts(self.live)
        self.refresh_chips(self.live)

    def refresh_os_chip(self, live):
        if live and not self.agent:
            self.os_chip.set_text('No guest tools')
            set_classes(self.os_chip, {'none'}, ('none',))
            self.os_chip.set_tooltip_text('Install the virtio-win guest tools: until then safe shutdown uses the power '
                                          'button and Windows details (IPs, users, C:) aren\'t available.')
            self.os_chip.show()
        elif self.os_chip.get_text() == 'No guest tools':
            self.os_chip.hide()

    def apply_checkpoints(self, rows):
        self.checkpoints = rows
        self.refresh_facts(self.live)

    def fill_detail(self):
        if not self.rev.get_child_revealed() and not self.rev.get_reveal_child():
            return
        f = self.facts
        info = (self.guest or {}).get('info') or {}
        live = self.state in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_PAUSED)
        sys_rows = []
        if self.guest and info.get('os.pretty-name'):
            build = info.get('os.kernel-release', '')
            rel = WIN_RELEASE.get(int(build), '') if str(build).isdigit() and 'Server' not in info.get('os.pretty-name', '') else ''
            sys_rows += [('Windows', info.get('os.pretty-name', ''), None),
                         ('Build', f'{build}' + (f' · {rel}' if rel else ''), None),
                         ('Host name', info.get('hostname', '—'), None)]
            n = int(info.get('user.count', 0) or 0)
            for i in range(n):
                who = f'{info.get(f"user.{i}.domain", "")}\\{info.get(f"user.{i}.name", "")}'.lstrip('\\')
                since = info.get(f'user.{i}.login-time')
                sys_rows.append(('Signed in' if i == 0 else '', who + (time.strftime(' · since %H:%M', time.localtime(since / 1000)) if since else ''), None))
            if not n:
                sys_rows.append(('Signed in', 'Nobody', 'muted'))
            d = self.guest.get('drift')
            if d is not None:
                sys_rows.append(('Clock', f'In sync ({abs(d):.1f} s)' if abs(d) < 60 else f'Off by {fmt_drift(d)}',
                                 None if abs(d) < 60 else 'bad'))
        else:
            sys_rows.append(('Windows', 'Start the VM to see Windows details' if not live else
                             'Install or start the QEMU guest agent to see Windows details', 'muted'))
        if live and self.up_since:
            sys_rows.append(('Powered on', time.strftime('%a %b %-d, %H:%M', time.localtime(self.up_since)), None))
        sys_rows += [('Firmware', ('UEFI' if f.get('efi') else 'BIOS') + (' · Secure Boot' if f.get('secure') else ''), None),
                     ('TPM', f'{f["tpm"]} (emulated)' if f.get('tpm') else 'None', None),
                     ('Display', ' · '.join(x for x in (f.get('video'), f.get('graphics')) if x) or '—', None),
                     ('Size', f'{f.get("vcpus", "?")} vCPU · {f.get("mem", 0) / GB:g} GB', None)]
        self._set_detail(self.d_sys, sys_rows)

        r = self.app.resources.rates.get(self.name, {}) if live else {}
        net_rows = []
        addrs = self.addresses()
        for i, nic in enumerate(f.get('nics', [])):
            kind = NET_KIND.get(nic['network'], ('', '', ''))[0]
            pre = '' if i == 0 else ' '
            net_rows.append((f'Network{pre}', f'{nic["network"]}' + (f' ({kind.lower()})' if kind else ''), None))
            ip, src = (addrs[i][1], addrs[i][2]) if i < len(addrs) else (None, '')
            net_rows.append((f'Address{pre}', f'{ip} · {"agent" if src == "guest agent" else "static"}' if ip else 'DHCP',
                             None if ip else 'muted'))
            net_rows.append((f'MAC{pre}', nic['mac'], None))
            nr = (r.get('nics') or {}).get(nic['vnet'])
            if live:
                net_rows.append((f'Device{pre}', f'{nic["vnet"] or "—"} · {nic["model"]}', None))
            if nr:
                net_rows.append((f'Since start{pre}', f'↓ {fmt_bytes(nr["rx_total"])}  ↑ {fmt_bytes(nr["tx_total"])}', None))
                net_rows.append((f'Live{pre}', f'↓ {fmt_rate(nr["rx"])}  ↑ {fmt_rate(nr["tx"])}', None))
                if nr.get('errs'):
                    net_rows.append((f'Errors{pre}', f'{nr["errs"]} errors / drops', 'bad'))
        self._set_detail(self.d_net, net_rows or [('Network', 'No network adapters', 'muted')])

        sto = []
        c = self.c_drive()
        if c and live:
            sto.append(('C:', f'{fmt_bytes(c[0])} used of {fmt_bytes(c[1])} ({c[0] / c[1] * 100:.0f}%)', None))
            for i in range(int(info.get('fs.count', 0) or 0)):
                mp = info.get(f'fs.{i}.mountpoint', '')
                if mp.upper().startswith('C:') or not re.match(r'^[A-Za-z]:', mp):
                    continue
                try:
                    sto.append((mp.rstrip('\\'), f'{info.get(f"fs.{i}.fstype", "")} · {fmt_bytes(int(info[f"fs.{i}.used-bytes"]))} '
                                f'of {fmt_bytes(int(info[f"fs.{i}.total-bytes"]))}', None))
                except (KeyError, ValueError):
                    pass
        if f.get('disk'):
            sto.append(('Image', os.path.basename(f['disk']), None))
        sto.append(('Virtual', self.capacity_text if f.get('disk') else '—', None))
        sto.append(('On host', fmt_bytes(self.disk_used), None))
        if live and r:
            sto.append(('Activity', f'R {fmt_rate(r.get("rd", 0))} · W {fmt_rate(r.get("wr", 0))}', None))
        if self.key == 'saved':
            sto.append(('Frozen image', fmt_bytes(self.saved_img[0]) if self.saved_img else 'saved', None))
        if self.checkpoints:
            for i, (nm, when) in enumerate(reversed(self.checkpoints[-3:])):
                sto.append(('Checkpoints' if i == 0 else '', f'{nm} · {when}'.rstrip(' ·'), None))
        else:
            sto.append(('Checkpoints', 'None' if self.checkpoints is not None else '…', 'muted'))
        self._set_detail(self.d_sto, sto)
        if not getattr(self.d_sto, 'manage', None):
            btn = styled(Gtk.Button(label='Manage checkpoints…', halign=Gtk.Align.START), 'link')
            btn.connect('clicked', lambda *_: self.open_checkpoints())
            self.d_sto.get_parent().pack_end(btn, False, False, 0)
            btn.show()
            self.d_sto.manage = btn

    def open_checkpoints(self):
        CheckpointDialog(self.app.window, self.dom, self.state).run_dialog()
        self.app.probe.poke(self.name)

    @property
    def running(self):
        return self.state == libvirt.VIR_DOMAIN_RUNNING

    @property
    def live(self):
        return self.state in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_PAUSED)

    def toggle_power(self):
        if self.state == libvirt.VIR_DOMAIN_SHUTOFF:
            self.app.start_vm(self.name)
        else:
            popover_menu(self.power, self.app.power_items(self.name, self.app.window)).popup()

    def show_menu(self):
        items = self.app.power_items(self.name, self.app.window) + [
            None,
            ('Checkpoints…', self.open_checkpoints, True),
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


LAB_RGBA = (*ACCENT, 0.95)
OTHER_RGBA = (0.62, 0.62, 0.62, 0.55)


def section_title(text, cap=None, extra=None):
    row = Gtk.Box(spacing=8)
    row.pack_start(styled(Gtk.Label(label=text, xalign=0), 'section'), False, False, 0)
    if extra is not None:
        row.pack_start(extra, False, False, 0)
    cap_label = None
    if cap is not None:
        cap_label = styled(Gtk.Label(label=cap, xalign=1, use_markup=True), 'section-cap')
        row.pack_end(cap_label, False, False, 0)
    return row, cap_label


def seg_control(options, active, cb):
    """A segmented control: radio buttons drawn as buttons."""
    box = styled(Gtk.Box(spacing=2, valign=Gtk.Align.CENTER), 'seg')
    group = None
    for key, label in options:
        b = Gtk.RadioButton.new_with_label_from_widget(group, label)
        b.set_mode(False)
        group = group or b
        b.set_active(key == active)
        b.connect('toggled', lambda btn, k=key: btn.get_active() and cb(k))
        box.pack_start(b, False, False, 0)
    return box


class ResourceView(Gtk.ScrolledWindow):
    """What the lab costs the host. Hero (rings, verdict, headroom, thread map, KPIs), insights,
    an activity timeline, where host RAM goes, a per-VM table, the lab networks and host facts.
    Numbers are host-side (what each VM's QEMU process really uses), collected from libvirt and
    /proc on every 2 s tick, also while the view is hidden, so history is there when it opens."""
    RESERVE = 4 * GB  # RAM to leave for Linux and your apps before calling it "room for another VM"
    HIST, HIST_STEP = 450, 2  # 15 minutes of 2 s samples
    STATS = (libvirt.VIR_DOMAIN_STATS_STATE | libvirt.VIR_DOMAIN_STATS_CPU_TOTAL | libvirt.VIR_DOMAIN_STATS_BALLOON
             | libvirt.VIR_DOMAIN_STATS_INTERFACE | libvirt.VIR_DOMAIN_STATS_BLOCK | libvirt.VIR_DOMAIN_STATS_VCPU)

    def __init__(self, app, names):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.app, self.names = app, names
        self.active = False
        self.prev = None
        self._fetching = False
        self.samples = 0  # real samples so far (history is pre-filled with zeros)
        self.rates = {}
        self.host = None
        self.mem_total = 0
        self.hover = self.focus = None
        self.ncpu = os.cpu_count() or 1
        self.model = ' '.join(re.sub(r'\((R|TM)\)|\bCPU\b|@.*|\d+(th|st|nd|rd) Gen', '', self._cpu_name()).split())
        self.temp_path, self.temp_crit = find_coretemp()
        self.temps = deque(maxlen=15)
        self.hist = {n: {k: deque([0.0] * self.HIST, maxlen=self.HIST) for k in ('cpu', 'rss', 'rd', 'wr', 'rx', 'tx')}
                     for n in names}
        self.hhist = {k: deque([0.0] * self.HIST, maxlen=self.HIST) for k in ('host', 'other_mem')}
        self.net_hist = {net: {k: deque([0.0] * 90, maxlen=90) for k in ('rx', 'tx')} for net in NET_KIND}
        self.ran = {n: self.HIST + 1 for n in names}  # samples since a VM was last running (for the chart legend)
        try:
            self.versions = (self._ver(app.conn.getLibVersion()), self._ver(app.conn.getVersion()))
        except libvirt.libvirtError:
            self.versions = ('?', '?')
        try:
            self.kernel = os.uname().release
        except OSError:
            self.kernel = ''

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=24, margin_end=24, margin_bottom=24, margin_top=8)
        self.add(body)
        self._build_hero(body)
        self._build_insights(body)
        self._build_activity(body)
        self._build_memory(body)
        self._build_table(body)
        self._build_networks(body)
        self._build_footer(body)

    @staticmethod
    def _ver(v):
        return f'{v // 1000000}.{v // 1000 % 1000}.{v % 1000}'

    @staticmethod
    def _cpu_name():
        try:
            with open('/proc/cpuinfo') as f:
                for line in f:
                    if line.startswith('model name'):
                        return line.split(':', 1)[1]
        except OSError:
            pass
        return ''

    # -- layout -------------------------------------------------------------
    def _build_hero(self, body):
        hero = styled(Gtk.Box(spacing=0), 'hero')
        body.pack_start(hero, False, False, 0)
        rings = Gtk.Box(spacing=8)
        self.rings = {}
        for key, title in (('cpu', 'Processor'), ('mem', 'Memory'), ('disk', 'Storage')):
            block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, halign=Gtk.Align.CENTER)
            block.set_size_request(140, -1)
            ov = Gtk.Overlay()
            gauge = RingGauge()
            ov.add(gauge)
            mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            mid.set_margin_top(8)
            val = styled(Gtk.Label(), 'ring-value')
            unit = styled(Gtk.Label(), 'ring-unit')
            mid.pack_start(val, False, False, 0)
            mid.pack_start(unit, False, False, 0)
            ov.add_overlay(mid)
            ov.set_overlay_pass_through(mid, True)
            block.pack_start(ov, False, False, 0)
            block.pack_start(styled(Gtk.Label(label=title), 'ring-title'), False, False, 0)
            legend = styled(Gtk.Label(use_markup=True, justify=Gtk.Justification.CENTER), 'ring-legend')
            block.pack_start(legend, False, False, 0)
            rings.pack_start(block, False, False, 0)
            self.rings[key] = (gauge, val, unit, legend)
        left = Gtk.Box()
        left.pack_start(rings, False, False, 0)
        left.pack_start(styled(Gtk.Box(), 'vhair'), False, False, 0)
        hero.pack_start(left, False, False, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER, hexpand=True)
        right.set_size_request(300, -1)
        self.verdict = styled(Gtk.Label(xalign=0, wrap=True), 'hero-title')
        self.hero_sub = styled(Gtk.Label(xalign=0, wrap=True), 'hero-sub')
        right.pack_start(self.verdict, False, False, 0)
        right.pack_start(self.hero_sub, False, False, 0)
        mid = Gtk.Box(spacing=28, margin_top=6)
        cap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        cap.pack_start(styled(Gtk.Label(label='CAPACITY', xalign=0), 'eyebrow'), False, False, 0)
        hr = Gtk.Box(spacing=8)
        self.headroom = styled(Gtk.Label(xalign=0), 'headroom-num')
        self.headroom_text = styled(Gtk.Label(xalign=0, valign=Gtk.Align.CENTER), 'headroom-text')
        hr.pack_start(self.headroom, False, False, 0)
        hr.pack_start(self.headroom_text, False, False, 0)
        cap.pack_start(hr, False, False, 0)
        mid.pack_start(cap, False, False, 0)
        tm = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        tm.pack_start(styled(Gtk.Label(label='HOST THREADS', xalign=0), 'eyebrow'), False, False, 0)
        self.threadmap = ThreadMap(self.ncpu)
        tm.pack_start(self.threadmap, False, False, 0)
        self.tm_cap = styled(Gtk.Label(xalign=0), 'ring-legend')
        tm.pack_start(self.tm_cap, False, False, 0)
        mid.pack_start(tm, False, False, 0)
        right.pack_start(mid, False, False, 0)
        kpis = Gtk.Box(spacing=8, homogeneous=True, margin_top=4)
        kpis.set_size_request(300, -1)
        self.kpi = {}
        for key, cap_text in (('over', 'Overcommit'), ('qemu', 'QEMU overhead'), ('temp', 'CPU package'), ('up', 'Host up')):
            tile = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2), 'kpi')
            tile.pack_start(styled(Gtk.Label(label=cap_text.upper(), xalign=0, ellipsize=Pango.EllipsizeMode.END),
                                   'kpi-cap'), False, False, 0)
            val = styled(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END), 'kpi-val')
            tile.pack_start(val, False, False, 0)
            if key == 'temp':
                self.tempbar = TempBar()
                self.tempbar.set_margin_top(3)
                tile.pack_start(self.tempbar, False, False, 0)
            kpis.pack_start(tile, True, True, 0)
            self.kpi[key] = (tile, val)
        right.pack_start(kpis, False, False, 0)
        hero.pack_start(right, True, True, 0)

    def _build_insights(self, body):
        row, self.insight_cap = section_title('Insights', '')
        body.pack_start(row, False, False, 0)
        self.insight_box = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), 'card', 'res', 'listgroup')
        body.pack_start(self.insight_box, False, False, 0)
        self.insight_rows = []
        self.insight_keys = None

    def _build_activity(self, body):
        controls = Gtk.Box(spacing=8)
        controls.pack_start(seg_control((('cpu', 'CPU'), ('mem', 'Memory'), ('disk', 'Disk'), ('net', 'Network')),
                                        'cpu', self._set_mode), False, False, 0)
        controls.pack_start(seg_control(((5, '5 min'), (15, '15 min')), 5, self._set_range), False, False, 0)
        row, _ = section_title('Activity')
        row.pack_end(controls, False, False, 0)
        body.pack_start(row, False, False, 0)
        card = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), 'card', 'res')
        body.pack_start(card, False, False, 0)
        self.timeline = Timeline(self)
        card.pack_start(self.timeline, False, False, 0)
        legend_row = Gtk.Box(spacing=8)
        legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=12, min_children_per_line=1,
                             column_spacing=6, row_spacing=6, homogeneous=False, hexpand=True, valign=Gtk.Align.START)
        legend_row.pack_start(legend, True, True, 0)
        self.legend_chips = {}
        for name in self.names + ['Other']:
            b = styled(Gtk.ToggleButton(), 'legend-chip')
            inner = Gtk.Box(spacing=6)
            sw = Swatch(vm_rgb(name) if name != 'Other' else (0.62, 0.62, 0.62), 8, round_=True)
            inner.pack_start(sw, False, False, 0)
            lbl = Gtk.Label(label=short(name))
            inner.pack_start(lbl, False, False, 0)
            b.add(inner)
            b.set_no_show_all(True)
            inner.show_all()
            if name != 'Other':
                b.connect('toggled', lambda btn, n=name: self._legend_toggled(btn, n))
                b.connect('enter-notify-event', lambda *_a, n=name: self.set_hover(n))
                b.connect('leave-notify-event', lambda *_a: self.set_hover(None))
            else:
                b.set_sensitive(False)
            legend.add(b)
            b.get_parent().set_can_focus(False)
            b.get_parent().set_no_show_all(True)
            self.legend_chips[name] = (b, lbl)
        self.scale_tag = styled(Gtk.Label(valign=Gtk.Align.START), 'scale-tag')
        legend_row.pack_end(self.scale_tag, False, False, 0)
        card.pack_start(legend_row, False, False, 0)

    def _build_memory(self, body):
        info = Gtk.Image.new_from_icon_name('dialog-information-symbolic', Gtk.IconSize.MENU)
        info.set_tooltip_text('Memory is what each VM really holds in host RAM (its QEMU process), including QEMU’s '
                              'own overhead. What Windows uses inside a VM isn’t visible: these VMs have no balloon device.')
        info.set_opacity(0.6)
        row, self.mem_cap = section_title('Where host RAM goes', '', extra=info)
        body.pack_start(row, False, False, 0)
        card = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6), 'card', 'res')
        body.pack_start(card, False, False, 0)
        self.rambar = RamBar(self)
        card.pack_start(self.rambar, False, False, 0)
        self.ram_legend = styled(Gtk.Label(xalign=0, use_markup=True, wrap=True), 'res-sub')
        card.pack_start(self.ram_legend, False, False, 0)

    COLS = (('VM', 150), ('CPU', 170), ('Memory on host', 140), ('Disk I/O', 120), ('Network', 120), ('Disk on host', 96))

    def _build_table(self, body):
        row, self.table_cap = section_title('Virtual machines', '')
        body.pack_start(row, False, False, 0)
        card = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), 'card', 'res', 'listgroup')
        body.pack_start(card, False, False, 0)
        self.size_groups = [Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL) for _ in self.COLS]
        head = styled(Gtk.Box(spacing=12), 'lg-head')
        for (title, width), sg in zip(self.COLS, self.size_groups):
            lbl = Gtk.Label(label=title.upper(), xalign=0)
            lbl.set_size_request(width, -1)
            sg.add_widget(lbl)
            head.pack_start(lbl, True, True, 0)
        card.pack_start(head, False, False, 0)
        self.trows = {}
        for name in self.names:
            card.pack_start(styled(Gtk.Box(), 'lg-sep'), False, False, 0)
            card.pack_start(self._table_row(name), False, False, 0)
        total = styled(Gtk.Box(spacing=12), 'res-total-row')
        self.total_cells = []
        for (_t, width), sg in zip(self.COLS, self.size_groups):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            cell.set_size_request(width, -1)
            main = styled(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END), 'res-total')
            sub = styled(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END), 'res-sub')
            cell.pack_start(main, False, False, 0)
            cell.pack_start(sub, False, False, 0)
            sg.add_widget(cell)
            total.pack_start(cell, True, True, 0)
            self.total_cells.append((main, sub))
        self.total_cells[0][0].set_text('Total')
        card.pack_start(total, False, False, 0)

    def _table_row(self, name):
        col = vm_rgb(name)
        ev = Gtk.EventBox()
        ev.set_tooltip_text('Click to highlight in the charts · double-click to connect · right-click for power')
        hb = styled(Gtk.Box(spacing=12), 'res-row')
        ev.add(hb)
        t = {'ev': ev, 'box': hb}

        def cell(i, widget):
            widget.set_size_request(self.COLS[i][1], -1)
            self.size_groups[i].add_widget(widget)
            hb.pack_start(widget, True, True, 0)

        c0 = Gtk.Box(spacing=8)
        t['swatch'] = Swatch(col, 10)
        c0.pack_start(t['swatch'], False, False, 0)
        t['icon'] = Gtk.Image.new_from_pixbuf(tile_pixbuf(LAB.get(name, ('client',))[0], False, size=28))
        c0.pack_start(t['icon'], False, False, 0)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, valign=Gtk.Align.CENTER)
        names.pack_start(styled(Gtk.Label(label=short(name), xalign=0), 'res-name'), False, False, 0)
        t['status'] = styled(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END), 'res-sub')
        names.pack_start(t['status'], False, False, 0)
        c0.pack_start(names, True, True, 0)
        cell(0, c0)

        def metric(i, visual):
            box = Gtk.Box(spacing=10)
            if visual is not None:
                box.pack_start(visual, False, False, 0)
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, valign=Gtk.Align.CENTER)
            main = styled(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END), 'res-main')
            sub = styled(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END), 'res-sub')
            txt.pack_start(main, False, False, 0)
            txt.pack_start(sub, False, False, 0)
            box.pack_start(txt, True, True, 0)
            cell(i, box)
            return main, sub

        t['spark'] = Sparkline(col, 72, 28, 60, scale='auto', dot=True)
        t['cpu'] = metric(1, t['spark'])
        t['membar'] = Meter(6, 96)
        t['mem'] = metric(2, None)
        # put the bar between the two lines
        main, sub = t['mem']
        parent = main.get_parent()
        parent.remove(sub)
        parent.pack_start(t['membar'], False, False, 2)
        parent.pack_start(sub, False, False, 0)
        t['dspark'] = MirrorSpark(col, 48, 28, 60)
        t['disk'] = metric(3, t['dspark'])
        t['nspark'] = MirrorSpark(col, 48, 28, 60)
        t['net'] = metric(4, t['nspark'])
        dbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, valign=Gtk.Align.CENTER)
        t['dsize'] = styled(Gtk.Label(xalign=0), 'res-main')
        t['dbar'] = Meter(3, 80)
        t['dsub'] = styled(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END), 'res-sub')
        for wdg in (t['dsize'], t['dbar'], t['dsub']):
            dbox.pack_start(wdg, False, False, 0)
        cell(5, dbox)

        ev.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)
        ev.connect('enter-notify-event', lambda *_: self.set_hover(name))
        ev.connect('leave-notify-event', lambda _w, e: e.detail != Gdk.NotifyType.INFERIOR and self.set_hover(None))
        ev.connect('button-press-event', lambda _w, e: self._row_click(name, ev, e))
        self.trows[name] = t
        return ev

    def _build_networks(self, body):
        row, _ = section_title('Lab networks')
        body.pack_start(row, False, False, 0)
        box = Gtk.Box(spacing=12, homogeneous=True)
        body.pack_start(box, False, False, 0)
        self.netcards = {}
        for net, (kind, cidr, dot) in NET_KIND.items():
            card = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8), 'card', 'res')
            head = Gtk.Box(spacing=8)
            head.pack_start(Swatch(rgb(dot), 8, round_=True), False, False, 0)
            head.pack_start(styled(Gtk.Label(label=net), 'net-title'), False, False, 0)
            head.pack_start(styled(Gtk.Label(label=f'{kind} · {cidr}'), 'net-tag'), False, False, 0)
            card.pack_start(head, False, False, 0)
            rate = styled(Gtk.Label(xalign=0), 'net-rate')
            cap = styled(Gtk.Label(xalign=0, label='between lab VMs' if kind == 'Isolated' else
                                   'internet through the host’s NAT (GW1 and CM1)'), 'res-sub')
            card.pack_start(rate, False, False, 0)
            card.pack_start(cap, False, False, 0)
            spark = MirrorSpark(rgb(dot), -1, 48, 90, label=True)
            spark.set_hexpand(True)
            card.pack_start(spark, False, False, 0)
            members = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=9,
                                  min_children_per_line=1, column_spacing=10, row_spacing=4, homogeneous=False)
            card.pack_start(members, False, False, 0)
            box.pack_start(card, True, True, 0)
            self.netcards[net] = {'rate': rate, 'spark': spark, 'members': members, 'member_set': None}

    def _build_footer(self, body):
        foot = styled(Gtk.Box(spacing=10), 'footer')
        foot.set_margin_top(4)
        body.pack_start(foot, False, False, 0)
        self.foot = {}
        for i, key in enumerate(('CPU', 'RAM', 'Load', 'Kernel', 'Virtualization')):
            if i:
                foot.pack_start(styled(Gtk.Label(label='·'), 'foot-key'), False, False, 0)
            foot.pack_start(styled(Gtk.Label(label=key), 'foot-key'), False, False, 0)
            val = styled(Gtk.Label(ellipsize=Pango.EllipsizeMode.END), 'foot-val')
            foot.pack_start(val, False, False, 0)
            self.foot[key] = val

    # -- interaction --------------------------------------------------------
    def set_hover(self, name):
        if name != self.hover:
            self.hover = name
            for n, t in self.trows.items():
                set_classes(t['box'], {'hl'} if n == name else set(), ('hl',))
            self.timeline.queue_draw()
            self.rambar.queue_draw()
        return False

    def set_focus(self, name):
        self.focus = None if self.focus == name else name
        for n, t in self.trows.items():
            set_classes(t['box'], {'focused'} if n == self.focus else set(), ('focused',))
        for n, (b, _l) in self.legend_chips.items():
            if n != 'Other' and b.get_active() != (n == self.focus):
                b.set_active(n == self.focus)
        self.timeline.queue_draw()
        self.rambar.queue_draw()

    def _legend_toggled(self, btn, name):
        if btn.get_active() != (self.focus == name):
            self.set_focus(name)

    def _row_click(self, name, widget, event):
        if event.button == 3:
            popover_menu(widget, self.app.power_items(name, self.app.window)).popup()
        elif event.type == Gdk.EventType._2BUTTON_PRESS:
            self.app.open_console(name)
        elif event.button == 1:
            self.set_focus(name)
        return True

    def _set_mode(self, mode):
        self.timeline.mode = mode
        self.timeline.ymax_shown = None
        self.render_activity()

    def _set_range(self, minutes):
        self.timeline.minutes = minutes
        self.render_activity()

    # -- history helpers (used by Timeline) ---------------------------------
    def window(self, dq, n, agg='mean'):
        vals = list(dq)[-n:]
        if n <= self.HIST // 3:
            return vals
        out = []
        for i in range(0, len(vals), 3):  # 15 min: 3-sample buckets, mean or max so spikes survive
            chunk = vals[i:i + 3]
            out.append(max(chunk) if agg == 'max' else sum(chunk) / len(chunk))
        return out

    def ever_ran(self, name, n):
        return self.ran.get(name, n + 1) <= n

    # -- data ---------------------------------------------------------------
    def collect(self):
        """Start one getAllDomainStats (it takes ~50 ms, so it runs on a worker thread) plus /proc
        reads; ingest() turns the result into rates, history and host numbers on the GTK thread."""
        if self._fetching:
            return
        self._fetching = True

        def fetch():
            # NOWAIT: a VM busy restoring a saved state doesn't stall everyone's numbers
            stats = self.app.conn.getAllDomainStats(self.STATS, libvirt.VIR_CONNECT_GET_ALL_DOMAINS_STATS_NOWAIT)
            return {d.name(): (d, s) for d, s in stats}, read_host(), time.monotonic()

        def done(res, err):
            self._fetching = False
            if err is None:
                self.ingest(*res)
                self.render()
            return False
        run_bg(fetch, done)

    def ingest(self, stats, host, now):
        if self.temp_path:
            try:
                with open(self.temp_path) as f:
                    self.temps.append(int(f.read()) / 1000)
            except (OSError, ValueError):
                pass
        prev, self.prev = self.prev, (now, host, stats)
        if not prev:
            return
        dt = max(0.2, now - prev[0])
        rates = {}
        nets = {net: [0.0, 0.0] for net in NET_KIND}
        for name in self.names:
            if name not in stats:  # deleted / undefined meanwhile: keep its history aligned
                for dq in self.hist[name].values():
                    dq.append(0.0)
                self.ran[name] = self.ran.get(name, self.HIST + 1) + 1
                continue
            dom, s = stats[name]
            row = self.app.rows.get(name)
            facts = row.facts if row else {}
            old = prev[2].get(name, (None, {}))[1]
            if old.get('state.state') == s.get('state.state'):
                for k, v in old.items():  # NOWAIT skips a busy VM's monitor stats: reuse the last values
                    s.setdefault(k, v)
            r = {'running': s.get('state.state') == libvirt.VIR_DOMAIN_RUNNING,
                 'vcpus': s.get('vcpu.current') or facts.get('vcpus') or 1,
                 'assigned': (s.get('balloon.current') or 0) * 1024 or facts.get('mem', 0)}
            if s.get('state.state', libvirt.VIR_DOMAIN_SHUTOFF) != libvirt.VIR_DOMAIN_SHUTOFF:
                r['rss'] = s.get('balloon.rss', 0) * 1024  # paused / crashed VMs still hold their RAM
            if r['running'] and old.get('state.state') == libvirt.VIR_DOMAIN_RUNNING:
                d = lambda k: max(0, s.get(k, 0) - old.get(k, 0)) / dt
                r['cpu'] = d('cpu.time') / 1e9  # threads busy
                r['vcpu_t'] = sum(d(f'vcpu.{i}.time') for i in range(r['vcpus'])) / 1e9
                r['cpu_pct'] = min(100.0, r['cpu'] / r['vcpus'] * 100)
                nb = s.get('block.count', 0)
                r['rd'] = sum(d(f'block.{i}.rd.bytes') for i in range(nb))
                r['wr'] = sum(d(f'block.{i}.wr.bytes') for i in range(nb))
                r['rx'] = r['tx'] = 0.0
                r['nics'] = {}
                vnet_net = {n['vnet']: n['network'] for n in facts.get('nics', [])}
                for i in range(s.get('net.count', 0)):
                    vn = s.get(f'net.{i}.name', '')
                    rx, tx = d(f'net.{i}.rx.bytes'), d(f'net.{i}.tx.bytes')
                    r['rx'] += rx
                    r['tx'] += tx
                    r['nics'][vn] = {'rx': rx, 'tx': tx, 'rx_total': s.get(f'net.{i}.rx.bytes', 0),
                                     'tx_total': s.get(f'net.{i}.tx.bytes', 0),
                                     'errs': sum(s.get(f'net.{i}.{k}', 0) for k in ('rx.errs', 'rx.drop', 'tx.errs', 'tx.drop'))}
                    net = vnet_net.get(vn)
                    if net in nets:
                        nets[net][0] += rx
                        nets[net][1] += tx
            rates[name] = r
            h = self.hist[name]
            h['cpu'].append(r.get('cpu', 0) / self.ncpu * 100)
            for k in ('rss', 'rd', 'wr', 'rx', 'tx'):
                h[k].append(r.get(k, 0.0))
            self.ran[name] = 0 if r['running'] else self.ran.get(name, self.HIST + 1) + 1
        self.rates = rates
        self.samples += 1
        span = max(1, host['total'] - prev[1]['total'])
        host_pct = (host['busy'] - prev[1]['busy']) / span * 100
        per = [(b1 - b0) / max(1, t1 - t0) * 100 for (b0, t0), (b1, t1) in zip(prev[1]['per'], host['per'])]
        mem = host['mem']
        lab_rss = sum(r.get('rss', 0) for r in rates.values())
        self.mem_total = mem['MemTotal']
        self.host = {'pct': host_pct, 'per': per, 'mem': mem, 'load': host['load'], 'uptime': host['uptime'],
                     'lab_rss': lab_rss}
        self.hhist['host'].append(host_pct)
        self.hhist['other_mem'].append(max(0, mem['MemTotal'] - mem['MemAvailable'] - lab_rss))
        for net, (rx, tx) in nets.items():
            # CorpNet: every frame between VMs is sent once, so the members' tx is the traffic.
            # InterNet: from the VMs' side, rx is downloads and tx uploads.
            self.net_hist[net]['rx'].append(rx)
            self.net_hist[net]['tx'].append(tx)

    def disk_fs(self):
        path = next((r.facts.get('disk') for r in self.app.rows.values() if r.facts.get('disk')), None)
        try:
            fs = os.statvfs(os.path.dirname(path) if path else '/')
            # total = what you can use (minus root's reserve), so the ring adds up
            return (fs.f_blocks - fs.f_bfree + fs.f_bavail) * fs.f_frsize, fs.f_bavail * fs.f_frsize
        except OSError:
            return 1, 0

    # -- render -------------------------------------------------------------
    def render(self):
        if not self.active or not self.host:
            return
        tot = self.totals()
        self.render_hero(tot)
        self.render_insights(tot)
        self.render_activity()
        self.render_memory(tot)
        self.render_table(tot)
        self.render_networks()
        self.render_footer()

    def totals(self):
        rows = self.app.rows
        t = dict(cpu=0.0, vcpu_t=0.0, vcpus=0, rss=0, assigned=0, overhead=0, rd=0.0, wr=0.0, rx=0.0, tx=0.0, running=0)
        for name, r in self.rates.items():
            if 'rss' in r:  # holds host RAM: running, paused or crashed
                t['rss'] += r['rss']
                t['assigned'] += r['assigned']
                t['overhead'] += max(0, r['rss'] - r['assigned'])
            if 'cpu' in r:
                for k in ('cpu', 'vcpu_t', 'rd', 'wr', 'rx', 'tx'):
                    t[k] += r[k]
                t['vcpus'] += r['vcpus']
                t['running'] += 1
        t['disk'] = sum(r.disk_used for r in rows.values())
        t['frozen'] = [(n, rows[n].facts.get('mem', 0)) for n in self.names if n in rows and rows[n].key == 'saved']
        t['off'] = sum(1 for n in self.names if n in rows and rows[n].key == libvirt.VIR_DOMAIN_SHUTOFF)
        mem = self.host['mem']
        t['total'], t['avail'] = mem['MemTotal'], mem['MemAvailable']
        t['other'] = max(0, t['total'] - t['avail'] - t['rss'])
        t['room'] = int(max(0, t['avail'] - self.RESERVE) // (4.3 * GB))
        t['fs_total'], t['fs_free'] = self.disk_fs()
        t['host_pct'] = self.host['pct']
        t['lab_pct'] = min(t['host_pct'], t['cpu'] / self.ncpu * 100)
        t['temp'] = sum(self.temps) / len(self.temps) if self.temps else None
        return t

    def render_hero(self, t):
        g, val, unit, legend = self.rings['cpu']
        g.set(t['lab_pct'] / 100, max(0, t['host_pct'] - t['lab_pct']) / 100)
        set_text(val, f'{t["host_pct"]:.0f}%')
        set_text(unit, 'CPU')
        set_text(legend, f'<span foreground="#60cdff">●</span> Lab {t["lab_pct"]:.1f}%\n'
                         f'<span foreground="#9e9e9e">●</span> Other {t["host_pct"] - t["lab_pct"]:.1f}%', markup=True)
        g, val, unit, legend = self.rings['mem']
        stops = (('#FF6B7D', '#FF8A96', '#FF99A4') if t['avail'] < self.RESERVE else
                 ('#FFD257', '#FCE100', '#FFF199') if t['avail'] < 8 * GB else None)
        g.set(t['rss'] / t['total'], t['other'] / t['total'], stops or ('#2F8FFF', '#60CDFF', '#B5ECFF'))
        set_text(val, f'{(t["total"] - t["avail"]) / GB:.1f}')
        set_text(unit, f'of {t["total"] / GB:.1f} GB')
        set_text(legend, f'<span foreground="#60cdff">●</span> Lab {fmt_bytes(t["rss"])}\n'
                         f'<span foreground="#9e9e9e">●</span> Other {fmt_bytes(t["other"])}', markup=True)
        g, val, unit, legend = self.rings['disk']
        fs_other = max(0, t['fs_total'] - t['fs_free'] - t['disk'])
        g.set(t['disk'] / t['fs_total'], fs_other / t['fs_total'])
        set_text(val, fmt_bytes(t['fs_free']).replace('.0 ', ' '))
        set_text(unit, f'free of {fmt_bytes(t["fs_total"])}')
        set_text(legend, f'<span foreground="#60cdff">●</span> Lab disks {fmt_bytes(t["disk"])}\n'
                         f'<span foreground="#9e9e9e">●</span> Other {fmt_bytes(fs_other)}', markup=True)

        insights = self.insights(t)
        worst = insights[0][0] if insights else 'ok'
        if not t['running']:
            verdict = 'Lab is idle'
        elif worst == 'crit' and any(i[1] == 'Memory' for i in insights if i[0] == 'crit'):
            verdict = 'Memory is critically low'
        elif worst in ('crit', 'warn'):
            verdict = insights[0][1]
        else:
            verdict = 'Lab is running comfortably'
        set_text(self.verdict, verdict)
        set_text(self.hero_sub, f'{t["running"]} of {len(self.names)} VMs running · {t["vcpus"]} vCPU on '
                                f'{self.ncpu} threads · {fmt_bytes(t["rss"])} in host RAM')
        set_text(self.headroom, str(t['room']))
        set_classes(self.headroom, {'crit'} if t['avail'] < self.RESERVE else {'warn'} if t['room'] == 0 else set(),
                    ('warn', 'crit'))
        set_text(self.headroom_text, f'more 4 GB client{"s" if t["room"] != 1 else ""}\nwith {fmt_bytes(self.RESERVE, 0)} kept free')
        self.threadmap.set(self.host['per'])
        set_text(self.tm_cap, f'{self.ncpu} threads · load {self.host["load"][0]:.2f}')
        ratio = t['vcpus'] / self.ncpu
        tile, v = self.kpi['over']
        set_text(v, f'{ratio:.2f} : 1')
        set_classes(v, {'warn'} if ratio > 1 else set(), ('warn',))
        tile.set_tooltip_text(f'{t["vcpus"]} vCPUs on {self.ncpu} host threads; {fmt_bytes(t["assigned"], 0)} of RAM '
                              f'assigned out of {fmt_bytes(t["total"], 0)}')
        tile, v = self.kpi['qemu']
        emu = max(0, t['cpu'] - t['vcpu_t'])
        set_text(v, fmt_bytes(t['overhead'], 0))
        tile.set_tooltip_text(f'What virtualization itself costs: {fmt_bytes(t["overhead"])} of QEMU memory beyond what '
                              f'the VMs were given, and {emu:.2f} threads of CPU spent emulating devices and doing I/O '
                              'rather than running the guests.')
        tile, v = self.kpi['temp']
        if t['temp'] is not None:
            set_text(v, f'{t["temp"]:.0f} °C')
            set_classes(v, {'warn'} if t['temp'] >= self.temp_crit - 3 else set(), ('warn',))
            self.tempbar.set((t['temp'] - 40) / max(1, self.temp_crit - 40))
            tile.set_tooltip_text(f'CPU package, 30 s average. Limit {self.temp_crit:.0f} °C: laptops often sit near it '
                                  'and slow down instead of failing.')
        else:
            set_text(v, '—')
        tile, v = self.kpi['up']
        set_text(v, fmt_span(self.host['uptime']))

    def insights(self, t):
        """[(severity, title, body, action)], worst first."""
        out = []
        rows = self.app.rows
        if t['running']:
            sev = 'crit' if t['avail'] < self.RESERVE else 'warn' if t['avail'] < 8 * GB else 'ok'
            swap = self.host['mem'].get('SwapTotal', 0)
            body = (f'{t["running"]} VMs hold {fmt_bytes(t["rss"])} ({t["rss"] / t["total"] * 100:.0f}%) · '
                    f'{fmt_bytes(t["overhead"], 0)} QEMU overhead · {fmt_bytes(t["avail"])} left'
                    + ('' if swap else ' · no swap, so if RAM runs out Linux stops the biggest VM (CM1)'))
            out.append((sev, 'Memory' if sev == 'ok' else 'Memory is running low', body,
                        ('Freeze lab', self.app.freeze_lab) if sev != 'ok' and not self.app.lab_run else None))
            emu = max(0.0, t['cpu'] - t['vcpu_t'])
            out.append(('info', f'Lab uses {t["lab_pct"]:.1f}% of the CPU',
                        f'{t["cpu"]:.2f} of {self.ncpu} threads busy; {emu:.2f} of that is QEMU overhead', None))
        if t['frozen']:
            need = sum(b for _n, b in t['frozen'])
            fits = need <= t['avail'] - self.RESERVE
            img = sum((rows[n].saved_img or (0, 0))[0] for n, _b in t['frozen'])
            out.append(('info' if fits else 'crit',
                        f'{len(t["frozen"])} VM{"s" if len(t["frozen"]) != 1 else ""} frozen'
                        + (f' · {fmt_bytes(img)} on disk' if img else ''),
                        f'Resuming needs {fmt_bytes(need, 0)} of RAM: '
                        + ('fits' if fits else 'doesn’t fit; freeze or stop something first'),
                        ('Resume lab', self.app.start_lab) if not self.app.lab_run else None))
        for name in self.names:
            row = rows.get(name)
            if not row:
                continue
            for sev, text, _tip, action in row.warnings():
                if text.startswith('Clock'):
                    out.append((sev, f'Clock drift on {short(name)}', f'{text}; domain sign-in fails at 5 min',
                                ('Sync clock', row.sync_clock)))
                elif text.startswith('C:'):
                    c = row.c_drive()
                    out.append((sev, f'C: almost full on {short(name)}',
                                f'{fmt_bytes(c[1] - c[0])} free of {fmt_bytes(c[1])}' if c else text, None))
                else:
                    out.append((sev, f'{text} on {short(name)}' if not text.startswith('Crashed') else f'{short(name)} crashed',
                                _tip, None))
        if t['fs_free'] < 50 * GB:
            out.append(('warn', 'Low disk space', 'VM disks and frozen memory images grow into the free space.', None))
        if t['temp'] is not None and t['temp'] >= self.temp_crit - 3:
            out.append(('info', f'CPU near its {self.temp_crit:.0f} °C limit',
                        'The laptop slows the CPU down to stay cool; VMs may run slower under load.', None))
        if t['vcpus'] > self.ncpu:
            out.append(('info', 'vCPUs share host threads', f'{t["vcpus"]} vCPUs on {self.ncpu} threads', None))
        if not any(s in ('warn', 'crit') for s, *_ in out):
            out.append(('ok', 'No problems found', f'Overcommit {t["vcpus"] / self.ncpu:.2f} : 1 · '
                        f'{fmt_bytes(t["assigned"], 0)} of {fmt_bytes(t["total"], 0)} assigned', None))
        order = {'crit': 0, 'warn': 1, 'info': 2, 'ok': 3}
        return sorted(out, key=lambda i: order[i[0]])[:6]

    def render_insights(self, t):
        items = self.insights(t)
        keys = [(i[0], i[1], i[3][0] if i[3] else None) for i in items]
        if keys != self.insight_keys:
            self.insight_keys = keys
            for child in self.insight_box.get_children():
                self.insight_box.remove(child)
            self.insight_rows = []
            for k, (sev, title, body, action) in enumerate(items):
                if k:
                    self.insight_box.pack_start(styled(Gtk.Box(), 'lg-sep'), False, False, 0)
                row = styled(Gtk.Box(spacing=12), 'insight')
                glyph = StatusGlyph(sev)
                row.pack_start(glyph, False, False, 0)
                txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
                tl = styled(Gtk.Label(label=title, xalign=0, wrap=True), 'insight-title')
                bl = styled(Gtk.Label(label=body, xalign=0, wrap=True), 'insight-body')
                txt.pack_start(tl, False, False, 0)
                txt.pack_start(bl, False, False, 0)
                row.pack_start(txt, True, True, 0)
                if action:
                    b = text_button(action[0], 'subtle', action[1])
                    row.pack_end(b, False, False, 0)
                self.insight_box.pack_start(row, False, False, 0)
                self.insight_rows.append((tl, bl))
            self.insight_box.show_all()
        else:
            for (tl, bl), (_s, title, body, _a) in zip(self.insight_rows, items):
                set_text(tl, title)
                set_text(bl, body)
        n_bad = sum(1 for i in items if i[0] in ('warn', 'crit'))
        set_text(self.insight_cap, f'{n_bad} need{"s" if n_bad == 1 else ""} attention' if n_bad else 'All clear')

    def render_activity(self):
        if not self.host:
            return
        self.timeline.queue_draw()
        mode = self.timeline.mode
        for name, (b, lbl) in self.legend_chips.items():
            if name == 'Other':
                b.set_visible(mode in ('cpu', 'mem'))
                b.get_parent().set_visible(mode in ('cpu', 'mem'))
                v = max(0.0, self.hhist['host'][-1] - sum(self.hist[n]['cpu'][-1] for n in self.names)) if mode == 'cpu' \
                    else self.hhist['other_mem'][-1]
                set_text(lbl, f'Other host  {v:.1f}%' if mode == 'cpu' else f'Linux & apps  {fmt_bytes(v)}')
                continue
            shown = self.ever_ran(name, self.HIST if self.timeline.minutes == 15 else self.HIST // 3)
            b.set_visible(shown)
            b.get_parent().set_visible(shown)
            h = self.hist[name]
            v = (f'{h["cpu"][-1]:.1f}%' if mode == 'cpu' else fmt_bytes(h['rss'][-1]) if mode == 'mem' else
                 fmt_rate(h['rd'][-1] + h['wr'][-1]) if mode == 'disk' else fmt_rate(h['rx'][-1] + h['tx'][-1]))
            set_text(lbl, f'{short(name)}  {v}')
        tl = self.timeline
        scale = (f'Scale {tl.ymax_shown:.0f}%' if mode == 'cpu' and tl.ymax_shown else
                 f'Scale {self.mem_total / GB:.0f} GB' if mode == 'mem' else 'Auto scale')
        set_text(self.scale_tag, scale)

    def render_memory(self, t):
        vms = [(n, min(self.rates[n]['rss'], self.rates[n]['assigned']))
               for n in self.names if n in self.rates and 'rss' in self.rates[n]]
        self.rambar.set({'total': t['total'], 'vms': vms, 'overhead': t['overhead'], 'other': t['other'],
                         'avail': t['avail'], 'reserve': self.RESERVE, 'frozen': t['frozen']})
        swap = self.host['mem'].get('SwapTotal', 0)
        set_text(self.mem_cap, f'{(t["total"] - t["avail"]) / GB:.1f} of {t["total"] / GB:.1f} GB in use · '
                               + ('swap ' + fmt_bytes(swap, 0) if swap else '<span foreground="#fce100">no swap</span>'),
                 markup=True)
        set_text(self.ram_legend,
                 f'<span foreground="#60cdff">■</span> Lab {fmt_bytes(t["rss"])}   '
                 f'<span foreground="#bbbbbb">▨</span> QEMU {fmt_bytes(t["overhead"], 0)}   '
                 f'<span foreground="#5a5a5a">■</span> Linux &amp; apps {fmt_bytes(t["other"])}   '
                 f'<span foreground="#555555">□</span> Free {fmt_bytes(t["avail"])} '
                 f'(<span foreground="#fce100">{fmt_bytes(self.RESERVE, 0)} kept for the host</span>)'
                 + (f'   <span foreground="#c3b1ff">❄ frozen: resuming needs {fmt_bytes(sum(b for _n, b in t["frozen"]), 0)}</span>'
                    if t['frozen'] else ''), markup=True)

    def render_table(self, t):
        rows = self.app.rows
        max_assigned = max([r.facts.get('mem', 0) for r in rows.values()] + [1])
        for name, tr in self.trows.items():
            row = rows.get(name)
            r = self.rates.get(name, {})
            live = 'cpu' in r
            key = row.key if row else None
            kind = LAB.get(name, ('client',))[0]
            if tr.get('key') != key:
                tr['key'] = key
                tr['icon'].set_from_pixbuf(tile_pixbuf(kind, key == libvirt.VIR_DOMAIN_RUNNING, frozen=key == 'saved', size=28))
                tr['swatch'].set_alpha(1.0 if key == libvirt.VIR_DOMAIN_RUNNING else 0.35)
            set_classes(tr['box'], set() if live else {'off'}, ('off',))
            for w in (tr['spark'], tr['membar'], tr['dspark'], tr['nspark']):
                w.set_visible(live)
            assigned = r.get('assigned') or (row.facts.get('mem', 0) if row else 0)
            vcpus = r.get('vcpus') or (row.facts.get('vcpus') if row else '?')
            if live:
                set_text(tr['status'], f'Running · {fmt_span(time.time() - row.up_since)}' if row and row.up_since else 'Running')
                h = self.hist[name]
                tr['spark'].set_values([v * self.ncpu / max(1, vcpus) for v in list(h['cpu'])[-60:]])
                set_text(tr['cpu'][0], f'{r["cpu_pct"]:.0f}%')
                set_text(tr['cpu'][1], f'{vcpus} vCPU · {r["cpu"] / self.ncpu * 100:.1f}% host')
                set_text(tr['mem'][0], fmt_bytes(r['rss']))
                tr['membar'].set([(min(r['rss'], assigned) / max_assigned, (*vm_rgb(name), 0.95)),
                                  (max(0, r['rss'] - assigned) / max_assigned, (1, 1, 1, 0.35))], tick=assigned / max_assigned)
                set_text(tr['mem'][1], f'{fmt_bytes(assigned, 0)} + {fmt_bytes(max(0, r["rss"] - assigned), 0)} QEMU')
                tr['dspark'].set_values(list(h['rd'])[-60:], list(h['wr'])[-60:])
                set_text(tr['disk'][0], f'R {fmt_rate(r["rd"])}')
                set_text(tr['disk'][1], f'W {fmt_rate(r["wr"])}')
                tr['nspark'].set_values(list(h['rx'])[-60:], list(h['tx'])[-60:])
                set_text(tr['net'][0], f'↓ {fmt_rate(r["rx"])}')
                set_text(tr['net'][1], f'↑ {fmt_rate(r["tx"])}')
                tr['ev'].set_tooltip_text(None)
            else:
                if row and row.empty_disk:
                    status = 'Empty disk · PXE target'
                elif key == 'saved':
                    status = f'Frozen {fmt_ago(row.saved_img[1])}' if row and row.saved_img else 'Frozen'
                elif key == libvirt.VIR_DOMAIN_PAUSED:
                    status = 'Paused'
                elif key == libvirt.VIR_DOMAIN_SHUTOFF:
                    status = f'Off · last used {fmt_ago(row.last_used)}' if row and row.last_used else 'Off'
                else:
                    status = STATE_TEXT.get(key, ('Unknown',))[0]
                set_text(tr['status'], status)
                set_text(tr['cpu'][0], '—')
                set_text(tr['cpu'][1], f'{vcpus} vCPU')
                set_text(tr['mem'][0], '—')
                set_text(tr['mem'][1], f'{fmt_bytes(assigned, 0)} assigned')
                for k in ('disk', 'net'):
                    set_text(tr[k][0], '—')
                    set_text(tr[k][1], '')
            used = row.disk_used if row else 0
            set_text(tr['dsize'], fmt_bytes(used) if used else '—')
            cap = (row.capacity if row else None) or 512 * GB
            tr['dbar'].set([(used / cap, (*vm_rgb(name), 0.9))])
            ck = row.checkpoints if row else None
            sub = ('+ ' + fmt_bytes(row.saved_img[0]) + ' saved RAM') if row and key == 'saved' and row.saved_img else \
                f'of {fmt_bytes(cap, 0)}' + (f' · {len(ck)} checkpoint{"s" if len(ck) != 1 else ""}' if ck else '')
            set_text(tr['dsub'], sub)
        c = self.total_cells
        set_text(c[0][1], f'{t["running"]} running')
        set_text(c[1][0], f'{t["lab_pct"]:.1f}% of host')
        set_text(c[1][1], f'{t["vcpus"]} vCPU · {t["cpu"]:.2f} threads busy')
        set_text(c[2][0], fmt_bytes(t['rss']))
        set_text(c[2][1], f'{fmt_bytes(t["assigned"], 0)} + {fmt_bytes(t["overhead"], 0)} QEMU')
        set_text(c[3][0], f'R {fmt_rate(t["rd"])}')
        set_text(c[3][1], f'W {fmt_rate(t["wr"])}')
        set_text(c[4][0], f'↓ {fmt_rate(t["rx"])}')
        set_text(c[4][1], f'↑ {fmt_rate(t["tx"])}')
        set_text(c[5][0], fmt_bytes(t['disk']))
        set_text(c[5][1], 'all lab disks')
        frozen = len(t['frozen'])
        set_text(self.table_cap, f'{t["running"]} running · {t["off"]} off' + (f' · {frozen} frozen' if frozen else '')
                 + f' · {fmt_bytes(t["disk"])} on disk')

    def render_networks(self):
        rows = self.app.rows
        for net, card in self.netcards.items():
            h = self.net_hist[net]
            rx, tx = h['rx'][-1], h['tx'][-1]
            if net == 'HYD-CorpNet':
                set_text(card['rate'], fmt_rate(tx))
            else:
                set_text(card['rate'], f'↓ {fmt_rate(rx)}   ↑ {fmt_rate(tx)}')
            card['spark'].set_values(h['rx'], h['tx'])
            members = [(n, rows[n].running) for n in self.names
                       if n in rows and any(nic['network'] == net for nic in rows[n].facts.get('nics', []))]
            if members != card['member_set']:
                card['member_set'] = members
                box = card['members']
                for child in box.get_children():
                    box.remove(child)
                for n, running in members:
                    m = Gtk.Box(spacing=4)
                    m.pack_start(Gtk.Image.new_from_pixbuf(tile_pixbuf(LAB.get(n, ('client',))[0], running, size=20)), False, False, 0)
                    m.pack_start(styled(Gtk.Label(label=short(n)), 'net-member'), False, False, 0)
                    if not running:
                        m.set_opacity(0.45)
                    box.add(m)
                    m.get_parent().set_can_focus(False)
                box.show_all()

    def render_footer(self):
        mem = self.host['mem']
        set_text(self.foot['CPU'], f'{self.model} · {self.ncpu} threads')
        swap = mem.get('SwapTotal', 0)
        set_text(self.foot['RAM'], f'{mem["MemTotal"] / GB:.1f} GB · ' + (f'swap {fmt_bytes(swap, 0)}' if swap else 'no swap'))
        set_text(self.foot['Load'], ' · '.join(f'{x:.2f}' for x in self.host['load']))
        set_text(self.foot['Kernel'], self.kernel)
        set_text(self.foot['Virtualization'], f'KVM · libvirt {self.versions[0]} · QEMU {self.versions[1]}')


class LabManager:
    def __init__(self, gapp=None):
        self.gapp = gapp  # Gtk.Application when run normally; None in tests driven by Gtk.main()
        self.conn = connect()
        self.consoles = {}
        self.window = None
        self.rows = {}
        self.lab_run = None  # while Start lab runs: {'cancel': Event, 'touched': set, 'status': str}
        self.probe = None
        self.resources = None

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
        self.view_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hhomogeneous=False)
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
        self.probe = GuestProbe(self)
        self.resources = ResourceView(self, names)
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
        self.view_stack.add_titled(self.resources, 'resources', 'Resources')
        self.view_stack.connect('notify::visible-child-name', self.on_view_changed)
        overlay.add(self.view_stack)
        win.add(overlay)
        win.connect('destroy', lambda *_: (self.grid.stop(), self.probe.stop(), setattr(self, 'window', None),
                                           self.maybe_quit()))
        self.window = win
        if self.gapp:
            self.gapp.add_window(win)
        GLib.timeout_add(2000, self.tick)
        win.show_all()
        self.tick()
        view = load_settings().get('view')
        if view in ('consoles', 'resources'):
            win.set_default_size(1180, 820)
            win.resize(1180, 820)
        if view in ('consoles', 'resources'):
            self.view_stack.set_visible_child_name(view)

    def on_view_changed(self, *_):
        view = self.view_stack.get_visible_child_name()
        self.grid.active = view == 'consoles'
        self.resources.active = view == 'resources'
        if self.resources.active:
            self.resources.render()
        if self.grid.active:
            self.grid.woken.clear()
            self.grid.refresh_states()
        if view in ('consoles', 'resources'):
            w, h = self.window.get_size()
            if w < 1100:  # room for three previews side by side / the dashboard
                self.window.resize(1180, max(h, 820))
        data = load_settings()
        data['view'] = view
        save_settings(data)

    def tick(self):
        if self.window is None:
            return False
        self.resources.collect()  # async: the rows show the rates from the previous sample
        for row in self.rows.values():
            row.refresh()
        if self.grid.active:
            self.grid.refresh_states()
        self.probe.publish({n: (r.running, bool(r.agent), r.up_since) for n, r in self.rows.items()})
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

    def on_probe(self, name, kind, data, err):
        row = self.rows.get(name)
        if row is None or err is not None or data is None:
            return False
        if kind == 'ckpt':
            row.apply_checkpoints(data)
        elif row.live:
            row.apply_guest(data)
        return False

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
                            libvirt_qemu.qemuAgentCommand(self.conn.lookupByName(n), '{"execute":"guest-ping"}', 3, 0)
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
        if self.lab_run:
            self.toast.show('Wait for the current lab run to finish', 3)
            return
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
