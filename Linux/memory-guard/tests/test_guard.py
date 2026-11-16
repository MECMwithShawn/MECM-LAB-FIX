"""Checks for the memory guard's decision rule. Run: python3 -B -m unittest discover -s Linux/memory-guard/tests"""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('guard', Path(__file__).resolve().parents[1] / 'vm-memory-guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
G = guard.GIB


class Decide(unittest.TestCase):
    def test_swap_in_use_with_plenty_of_memory_is_not_low(self):
        # the false alarm: 19.1 GB available, 4.2 GB of 32 GB swap in use
        self.assertEqual(guard.decide(int(19.1 * G), 32 * G, int(4.2 * G), None), 'none')
        self.assertEqual(guard.decide(19 * G, 32 * G, 30 * G, None), 'none')

    def test_first_dip_pauses_one(self):
        self.assertEqual(guard.decide(int(5.9 * G), 32 * G, 0, None), 'one')

    def test_no_second_pause_until_memory_falls_further(self):
        floor = int(5.9 * G)
        self.assertEqual(guard.decide(int(5.5 * G), 32 * G, 0, floor), 'none')
        self.assertEqual(guard.decide(int(4.9 * G), 32 * G, 0, floor), 'one')

    def test_last_resort_pauses_all(self):
        self.assertEqual(guard.decide(int(1.5 * G), 32 * G, 20 * G, int(2.4 * G)), 'all')
        self.assertEqual(guard.decide(int(1.5 * G), 0, 0, None), 'all')  # no swap at all

    def test_nearly_out_but_swap_has_room_is_staged(self):
        self.assertEqual(guard.decide(int(1.5 * G), 32 * G, 2 * G, int(2.4 * G)), 'none')
        self.assertEqual(guard.decide(int(1.3 * G), 32 * G, 2 * G, int(2.4 * G)), 'one')


if __name__ == '__main__':
    unittest.main()
