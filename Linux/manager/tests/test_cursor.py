"""Checks for the console cursor fix.

The desktop draws garbage under any cursor image larger than 64 pixels a side, so the fix must
always produce a smaller one, without moving the click point. Extracts the helpers without
starting the application; the surface test needs PyGObject and pycairo but no display.
Run with python3 -B -m unittest discover -s Linux/manager/tests.
"""
import ast
import math
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

try:
    import cairo
    import gi
    gi.require_version('Gdk', '3.0')
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import Gdk, GdkPixbuf, GLib
except (ImportError, ValueError):  # pragma: no cover - the app itself needs these
    cairo = Gdk = GdkPixbuf = GLib = None

SOURCE = Path(os.environ.get('LAB_MANAGER_TEST_SOURCE',
                            str(Path(__file__).resolve().parents[1] / 'labmanager.py')))
TREE = ast.parse(SOURCE.read_text())
NAMES = {'cursor_shape_fields', 'opaque_bounds', 'cursor_plan', 'cursor_surface'}
NODES = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]
LIMIT = next(ast.literal_eval(node.value) for node in TREE.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == 'CURSOR_LIMIT' for target in node.targets))
NS = {'math': math, 'cairo': cairo, 'Gdk': Gdk, 'GdkPixbuf': GdkPixbuf, 'GLib': GLib, 'CURSOR_LIMIT': LIMIT}
exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), 'exec'), NS)

# The Windows 11 arrow as a lab VM sends it: 41x39 RGBA, hotspot (3, 3), arrow and shadow in the
# top-left 19x23 or so, the rest transparent.
ARROW = (Path(__file__).parent / 'arrow-41x39.rgba').read_bytes()


class ShapeFieldTests(unittest.TestCase):
    def test_shifted_layout_from_pygobject(self):
        # type 0, width 41, height 39 packed into the 8 bytes PyGObject reads as `type`;
        # the hotspot (3, 3) shows up under the names width and height
        shape = SimpleNamespace(type=(39 << 48) | (41 << 32), width=3, height=3, hot_spot_x=0, hot_spot_y=0)
        self.assertEqual(NS['cursor_shape_fields'](shape), (41, 39, 3, 3))
        mono = SimpleNamespace(type=(32 << 48) | (32 << 32) | 1, width=8, height=9, hot_spot_x=0, hot_spot_y=0)
        self.assertEqual(NS['cursor_shape_fields'](mono), (32, 32, 8, 9))

    def test_correct_layout_is_left_alone(self):
        shape = SimpleNamespace(type=1, width=32, height=32, hot_spot_x=8, hot_spot_y=9)
        self.assertEqual(NS['cursor_shape_fields'](shape), (32, 32, 8, 9))


class BoundsTests(unittest.TestCase):
    def test_real_arrow_is_cropped_to_its_visible_part(self):
        x0, y0, x1, y1 = NS['opaque_bounds'](ARROW, 41, 39, 3, 3)
        self.assertLessEqual((x0, y0), (3, 3))            # hotspot stays inside
        self.assertLess(x1 - x0, 24)
        self.assertLess(y1 - y0, 28)
        alpha = ARROW[3::4]
        for y in range(39):                                # nothing visible is cut off
            for x in range(41):
                if alpha[y * 41 + x]:
                    self.assertTrue(x0 <= x < x1 and y0 <= y < y1)

    def test_blank_cursor_keeps_one_pixel_at_the_hotspot(self):
        self.assertEqual(NS['opaque_bounds'](bytes(8 * 8 * 4), 8, 8, 5, 2), (5, 2, 6, 3))

    def test_hotspot_outside_the_drawing_is_included(self):
        rgba = bytearray(8 * 8 * 4)
        rgba[(6 * 8 + 6) * 4 + 3] = 255                    # one pixel at (6, 6)
        self.assertEqual(NS['opaque_bounds'](bytes(rgba), 8, 8, 1, 1), (1, 1, 7, 7))


class PlanTests(unittest.TestCase):
    def test_never_over_the_limit_and_fits_the_scale(self):
        for scale_factor in (1, 2, 3):
            for size in ((19, 23), (32, 32), (41, 39), (64, 64), (128, 128), (1, 1)):
                for zoom in (.5, 1, 1.328, 1.719, 2.07, 3, 6):
                    scale, width, height, per_pixel = NS['cursor_plan'](*size, zoom, scale_factor)
                    with self.subTest(scale_factor=scale_factor, size=size, zoom=zoom):
                        self.assertLessEqual(max(width, height), LIMIT)
                        self.assertEqual((width % scale, height % scale), (0, 0))   # GDK needs this
                        self.assertGreater(min(width, height), 0)
                        self.assertGreaterEqual(width + 1e-6, size[0] * per_pixel)   # drawing fits
                        self.assertGreaterEqual(height + 1e-6, size[1] * per_pixel)

    def test_size_on_screen_is_kept_while_it_fits(self):
        # the cropped arrow at the zoom from the bug report: full sharpness
        scale, width, height, per_pixel = NS['cursor_plan'](19, 23, 1.719, 2)
        self.assertEqual(scale, 2)
        self.assertAlmostEqual(per_pixel / scale, 1.719 / 2)
        # too big at scale 2: same size on screen, drawn at scale 1
        scale, width, height, per_pixel = NS['cursor_plan'](41, 39, 2.5, 2)
        self.assertEqual(scale, 1)
        self.assertAlmostEqual(per_pixel / scale, 2.5 / 2)

    def test_uncropped_arrow_at_the_reported_zoom_would_not_fit(self):
        # what the SPICE widget does: 41x39 at 1.719 is 70x67, over the limit
        self.assertGreater(int(41 * 1.719), LIMIT)


@unittest.skipIf(cairo is None, 'PyGObject / pycairo not available')
class SurfaceTests(unittest.TestCase):
    def test_real_arrow_surface(self):
        surface, hot = NS['cursor_surface'](ARROW, 41, 39, 3, 3, 1.719, 2)
        self.assertLessEqual(max(surface.get_width(), surface.get_height()), LIMIT)
        self.assertEqual(surface.get_device_scale(), (2, 2))
        self.assertEqual((surface.get_width() % 2, surface.get_height() % 2), (0, 0))
        x0, y0, _x1, _y1 = NS['opaque_bounds'](ARROW, 41, 39, 3, 3)
        self.assertAlmostEqual(hot[0], (3 - x0) * 1.719 / 2)
        self.assertAlmostEqual(hot[1], (3 - y0) * 1.719 / 2)
        surface.flush()
        data, stride = surface.get_data(), surface.get_stride()
        opaque = sum(1 for y in range(surface.get_height()) for x in range(surface.get_width())
                     if data[y * stride + x * 4 + 3] > 200)
        self.assertGreater(opaque, 150)                    # the arrow really is drawn


if __name__ == '__main__':
    unittest.main()
