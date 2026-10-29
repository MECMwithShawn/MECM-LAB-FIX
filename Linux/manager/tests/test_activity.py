"""Display-free checks for Activity geometry and telemetry presentation.

Extract the drawing helpers without importing GTK, opening libvirt or starting
the application. Run with python3 -B -m unittest discover -s Linux/manager/tests.
"""
import ast
from collections import deque
import math
import os
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(os.environ.get('LAB_MANAGER_TEST_SOURCE',
                            str(Path(__file__).resolve().parents[1] / 'labmanager.py')))
TREE = ast.parse(SOURCE.read_text())
NAMES = {'rgb', 'vm_rgb', 'timeline_points', 'timeline_controls', 'timeline_runs', 'timeline_age', 'timeline_sum', 'Timeline'}
NODES = [node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in NAMES]
PALETTE = next(ast.literal_eval(node.value) for node in TREE.body if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == 'VM_COLORS' for target in node.targets))
NS = {'math': math, 'Gtk': SimpleNamespace(DrawingArea=object), 'VM_COLORS': PALETTE}
exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), 'exec'), NS)


def fixture(samples=3):
    names = ['HYD-DC1', 'HYD-GW1']
    keys = ('cpu', 'rss', 'rd', 'wr', 'rx', 'tx')
    view = SimpleNamespace(HIST=450, HIST_STEP=2, samples=samples, names=names,
                           hist={name: {key: deque([0.0] * 450, maxlen=450) for key in keys} for name in names},
                           hhist={key: deque([0.0] * 450, maxlen=450) for key in ('host', 'host_mem', 'other_mem')},
                           sample_times=deque((100 + i * 2 for i in range(samples)), maxlen=450),
                           ever_ran=lambda _name, _count: True)
    for name, cpu in zip(names, (2.0, 3.0)):
        for _ in range(samples):
            for key in keys:
                view.hist[name][key].append(cpu)
    for _ in range(samples):
        view.hhist['host'].append(11.0)
        view.hhist['host_mem'].append(19.0)
        view.hhist['other_mem'].append(7.0)
    chart = object.__new__(NS['Timeline'])
    chart.view, chart.mode, chart.minutes = view, 'cpu', 5
    return chart, view


class CurveTests(unittest.TestCase):
    def test_subpixel_simplification_retains_extrema_and_plateau_edges(self):
        points = [(0, 0), (1, .01), (2, .02), (3, 4), (4, 4), (5, 4), (6, .01), (7, 0), (8, .01)]
        reduced = NS['timeline_points'](points)
        for index in (0, 3, 5, 7, 8):
            self.assertIn(points[index], reduced)
        self.assertEqual(points[1], (1, .01))  # simplification never changes raw samples

    def test_flat_geometry_is_reduced_to_two_endpoints(self):
        points = [(i, 0.0) for i in range(450)]
        self.assertEqual(NS['timeline_points'](points), [points[0], points[-1]])

    def test_curves_preserve_samples_and_do_not_overshoot(self):
        points = [(0, 0), (.3, 8), (2, .1), (3, 12), (4.5, 12), (8, 0)]
        controls = NS['timeline_controls'](points)
        for a, b, controls in zip(points, points[1:], controls):
            x1, y1, x2, y2, x3, y3 = controls
            self.assertEqual((x3, y3), b)
            for step in range(101):
                t = step / 100
                blend = lambda p0, p1, p2, p3: (1-t)**3*p0 + 3*(1-t)**2*t*p1 + 3*(1-t)*t*t*p2 + t**3*p3
                x = blend(a[0], x1, x2, x3)
                y = blend(a[1], y1, y2, y3)
                self.assertTrue(a[0]-1e-10 <= x <= b[0]+1e-10)
                self.assertTrue(min(a[1], b[1])-1e-10 <= y <= max(a[1], b[1])+1e-10)

    def test_flat_segments_remain_flat(self):
        controls = NS['timeline_controls']([(0, 5), (1, 5), (8, 5)])
        for segment in controls:
            self.assertEqual(segment[1::2], (5, 5, 5))

    def test_empty_and_single_samples_have_no_invented_segment(self):
        self.assertEqual(NS['timeline_controls']([]), [])
        self.assertEqual(NS['timeline_controls']([(3, 9)]), [])

    def test_nonincreasing_timestamps_are_rejected(self):
        with self.assertRaises(ValueError):
            NS['timeline_controls']([(2, 4), (2, 5)])

    def test_missing_samples_and_time_gaps_break_paths(self):
        times = [0, 2, 4, 6, 8, 30, 32, 34]
        values = [1, 3, None, 5, float('nan'), 2, 4, 5]
        self.assertEqual(NS['timeline_runs'](times, values, 6), [[0, 1], [3], [5, 6, 7]])

    def test_backwards_time_starts_a_new_path(self):
        self.assertEqual(NS['timeline_runs']([0, 2, 1, 3], [1, 2, 3, 4], 6), [[0, 1], [2, 3]])

    def test_relative_time_boundaries(self):
        for seconds, expected in ((0, 'Now'), (18, '18 s ago'), (60, '1 min ago'),
                                  (78, '1 min 18 s ago'), (300, '5 min ago')):
            with self.subTest(seconds=seconds):
                self.assertEqual(NS['timeline_age'](seconds), expected)


class HistoryTests(unittest.TestCase):
    def test_empty_history_does_not_draw_prefilled_zeroes(self):
        chart, _view = fixture(0)
        layers, extra, _kind = chart.series()
        self.assertEqual(chart._times, [])
        self.assertEqual(extra['host'], [])
        self.assertTrue(all(not layer[2] for layer in layers))

    def test_one_sample_stays_one_sample(self):
        chart, _view = fixture(1)
        layers, extra, _kind = chart.series()
        self.assertEqual(chart._times, [100])
        self.assertEqual(extra['host'], [11.0])
        self.assertTrue(all(len(layer[2]) == 1 for layer in layers))

    def test_cpu_total_is_not_reconstructed_from_vm_values(self):
        chart, _view = fixture()
        _layers, extra, _kind = chart.series()
        self.assertEqual(extra['host'], [11.0] * 3)
        self.assertEqual(extra['other'], [6.0] * 3)

    def test_memory_uses_independent_host_series(self):
        chart, _view = fixture()
        chart.mode = 'mem'
        _layers, extra, _kind = chart.series()
        self.assertEqual(extra['host'], [19.0] * 3)
        self.assertEqual(extra['other'], [7.0] * 3)
        self.assertFalse(extra['derived'])

    def test_fallback_memory_aggregate_is_explicitly_derived(self):
        chart, view = fixture()
        del view.hhist['host_mem']
        chart.mode = 'mem'
        _layers, extra, _kind = chart.series()
        self.assertEqual(extra['host'], [12.0] * 3)
        self.assertTrue(extra['derived'])

    def test_fifteen_minutes_keep_spikes_and_original_timestamps(self):
        chart, view = fixture(450)
        chart.minutes = 15
        view.hist['HYD-DC1']['cpu'][71] = 83.0
        layers, _extra, _kind = chart.series()
        self.assertEqual(len(layers[0][2]), 450)
        self.assertEqual(layers[0][2][71], 83.0)
        self.assertEqual(chart._times[71], 242)

    def test_five_minutes_select_the_recent_raw_window(self):
        chart, _view = fixture(450)
        layers, _extra, _kind = chart.series()
        self.assertEqual(len(layers[0][2]), 150)
        self.assertEqual(chart._times, list(range(700, 1000, 2)))

    def test_elapsed_window_excludes_expired_spikes_from_every_aligned_series(self):
        chart, view = fixture(5)
        view.sample_times = deque([2, 4, 6, 608, 610], maxlen=450)
        view.hhist['host'] = deque([100, 100, 100, 10, 10], maxlen=450)
        view.hhist['host_mem'] = deque([90, 90, 90, 20, 21], maxlen=450)
        view.hhist['other_mem'] = deque([70, 70, 70, 10, 11], maxlen=450)
        for name in view.names:
            for key in view.hist[name]:
                view.hist[name][key] = deque([80, 80, 80, 3, 4], maxlen=450)
        for mode in ('cpu', 'mem', 'disk', 'net'):
            chart.mode = mode
            with self.subTest(mode=mode):
                layers, extra, _kind = chart.series()
                self.assertEqual(chart._times, [608, 610])
                self.assertTrue(all(side == [3, 4] for layer in layers for side in layer[2:]))
                self.assertTrue(all(len(values) == 2 for values in extra.values() if isinstance(values, list)))
                if mode == 'cpu':
                    self.assertEqual(extra['host'], [10, 10])
                    self.assertEqual(extra['other'], [4, 2])
                elif mode == 'mem':
                    self.assertEqual(extra['host'], [20, 21])
                    self.assertEqual(extra['other'], [10, 11])
                else:
                    self.assertEqual(extra['up'], [6, 8])
                    self.assertEqual(extra['down'], [6, 8])

    def test_time_window_includes_boundary_and_honors_selected_range(self):
        chart, view = fixture(4)
        view.sample_times = deque([309, 310, 608, 610], maxlen=450)
        _layers, extra, _kind = chart.series()
        self.assertEqual(chart._times, [310, 608, 610])
        self.assertEqual(len(extra['host']), 3)
        chart.minutes = 15
        _layers, extra, _kind = chart.series()
        self.assertEqual(chart._times, [309, 310, 608, 610])
        self.assertEqual(len(extra['host']), 4)

    def test_clock_regression_uses_newest_segment_without_reordering(self):
        chart, view = fixture(5)
        view.sample_times = deque([500, 502, 100, 102, 104], maxlen=450)
        view.hhist['host'] = deque([90, 90, 10, 12, 14], maxlen=450)
        _layers, extra, _kind = chart.series()
        self.assertEqual(chart._times, [100, 102, 104])
        self.assertEqual(extra['host'], [10, 12, 14])

    def test_duplicate_timestamp_keeps_latest_reading_with_alignment(self):
        chart, view = fixture(4)
        view.sample_times = deque([100, 102, 102, 104], maxlen=450)
        view.hhist['host'] = deque([10, 80, 12, 14], maxlen=450)
        view.hist['HYD-DC1']['cpu'] = deque([1, 70, 2, 3], maxlen=450)
        layers, extra, _kind = chart.series()
        self.assertEqual(chart._times, [100, 102, 104])
        self.assertEqual(extra['host'], [10, 12, 14])
        self.assertEqual(layers[0][2], [1, 2, 3])

    def test_io_totals_preserve_direction_and_missing_values(self):
        chart, view = fixture()
        chart.mode = 'disk'
        view.hist['HYD-GW1']['wr'][-1] = 9.0
        view.hist['HYD-DC1']['rd'][-2] = None
        layers, extra, kind = chart.series()
        self.assertEqual(kind, 'mirror')
        self.assertEqual(extra['up'], [5.0, None, 5.0])
        self.assertEqual(extra['down'], [5.0, 5.0, 11.0])
        self.assertIsNone(layers[0][2][-2])


if __name__ == '__main__':
    unittest.main()
