"""Run with GDK_BACKEND=x11 xvfb-run -a python3 -B Linux/manager/tests/check_motion.py."""
import importlib.util
from pathlib import Path
p=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('fixture',p/'tests/render_fixture.py'); f=importlib.util.module_from_spec(s);s.loader.exec_module(f)
m=f.load_app(p/'labmanager.py');a=f.setup(m);f.seed(m,a);a.view_stack.set_visible_child_name('resources');f.pump(m)
m.Gtk.Settings.get_default().set_property('gtk-enable-animations',True)
c=a.resources.timeline
surface=m.cairo.ImageSurface(m.cairo.FORMAT_ARGB32,c.get_allocated_width(),c.get_allocated_height());cr=m.cairo.Context(surface)
original=m.time.monotonic
positions=[]
c._sample_stamp = None
c._arrival = None
try:
 for elapsed in (0,.25,.5,1,1.5,2,3):
  m.time.monotonic=lambda:1000+elapsed
  c._draw(c,cr);positions.append(c._plot['positions'][-1])
 assert all(x>y for x,y in zip(positions[:5],positions[1:6])),positions
 assert positions[5]==positions[6],positions
 assert a.resources.samples==150
 print('Smooth motion between samples verified; stale data stops scrolling; raw samples unchanged:',positions)
finally:
 m.time.monotonic=original
 a.window.destroy()
