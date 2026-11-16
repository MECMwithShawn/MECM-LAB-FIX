"""Checks for the console cursor fix: the padded cursor must always be a size GDK accepts.

Extracts the two pure helpers without starting the application. Needs PyGObject and pycairo but
no display. Run with python3 -B -m unittest discover -s Linux/manager/tests.
"""
import ast
import os
from pathlib import Path
import unittest

try:
    import cairo
    import gi
    gi.require_version('Gdk', '3.0')
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import Gdk, GdkPixbuf, GLib
except (ImportError, ValueError):  # pragma: no cover - the app itself needs these
    cairo = None

SOURCE = Path(os.environ.get('LAB_MANAGER_TEST_SOURCE',
                            str(Path(__file__).resolve().parents[1] / 'labmanager.py')))
NAMES = {'cursor_fits', 'padded_cursor_surface'}
NODES = [node for node in ast.parse(SOURCE.read_text()).body
         if isinstance(node, ast.FunctionDef) and node.name in NAMES]
NS = {'cairo': cairo, 'Gdk': Gdk if cairo else None}
exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), 'exec'), NS)


def solid(width, height):
    """An opaque white guest cursor image."""
    data = bytes([255]) * (width * height * 4)
    return GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB, True, 8,
                                           width, height, width * 4)


def alpha(surface, x, y):
    surface.flush()
    return surface.get_data()[y * surface.get_stride() + x * 4 + 3]


@unittest.skipIf(cairo is None, 'PyGObject / pycairo not available')
class CursorFixTests(unittest.TestCase):
    def test_fits(self):
        self.assertTrue(NS['cursor_fits'](85, 81, 1))
        self.assertFalse(NS['cursor_fits'](85, 81, 2))   # the size from the bug report
        self.assertFalse(NS['cursor_fits'](86, 81, 2))
        self.assertTrue(NS['cursor_fits'](86, 82, 2))

    def test_padded_size_is_a_multiple_of_the_scale(self):
        for scale in (1, 2, 3):
            for width, height, zoom in ((32, 32, 2.66), (41, 39, 1.719), (64, 61, 1.328), (3, 3, .5), (1, 1, .5)):
                surface = NS['padded_cursor_surface'](solid(width, height), zoom, scale)
                with self.subTest(scale=scale, size=(width, height), zoom=zoom):
                    self.assertTrue(NS['cursor_fits'](surface.get_width(), surface.get_height(), scale))
                    drawn = max(1, int(zoom * width))
                    self.assertGreaterEqual(surface.get_width(), drawn)
                    self.assertLess(surface.get_width() - drawn, scale)
                    self.assertEqual(surface.get_device_scale(), (scale, scale))

    def test_image_keeps_its_size_and_padding_is_transparent(self):
        zoom = 1.719   # a 41x39 guest cursor becomes 70x67, padded to 70x68
        surface = NS['padded_cursor_surface'](solid(41, 39), zoom, 2)
        width, height = int(zoom * 41), int(zoom * 39)
        self.assertEqual((surface.get_width(), surface.get_height()), (70, 68))
        self.assertEqual(alpha(surface, 2, 2), 255)
        self.assertEqual(alpha(surface, width - 3, height - 3), 255)
        if surface.get_height() > height:
            self.assertEqual(alpha(surface, 2, surface.get_height() - 1), 0)


if __name__ == '__main__':
    unittest.main()
