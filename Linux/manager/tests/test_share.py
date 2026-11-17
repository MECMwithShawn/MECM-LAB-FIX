"""Checks for the host share helpers that need no VM. Run: python3 -B -m unittest discover -s Linux/manager/tests"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location('labshare', Path(__file__).resolve().parents[1] / 'labshare.py')
labshare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(labshare)

PLAIN = "<domain type='kvm'><name>x</name><memory unit='KiB'>1024</memory><vcpu>1</vcpu><devices/></domain>"


class Definition(unittest.TestCase):
    def test_shared_memory_is_added_once_after_memory(self):
        xml = labshare.with_shared_memory(PLAIN)
        self.assertTrue(labshare.has_shared_memory(xml))
        self.assertEqual([c.tag for c in ET.fromstring(xml)][:3], ['name', 'memory', 'memoryBacking'])
        self.assertEqual(labshare.with_shared_memory(xml), xml)

    def test_other_memory_backing_is_replaced(self):
        xml = PLAIN.replace('<vcpu>', '<memoryBacking><locked/></memoryBacking><vcpu>')
        out = labshare.with_shared_memory(xml)
        self.assertTrue(labshare.has_shared_memory(out))
        self.assertEqual(out.count('<memoryBacking>'), 1)

    def test_share_is_recognised_by_its_tag(self):
        self.assertFalse(labshare.has_share(PLAIN))
        dev = labshare.device_xml('x')
        self.assertTrue(labshare.has_share(PLAIN.replace('<devices/>', f'<devices>{dev}</devices>')))
        other = dev.replace(labshare.TAG, 'something-else')
        self.assertFalse(labshare.has_share(PLAIN.replace('<devices/>', f'<devices>{other}</devices>')))


class Server(unittest.TestCase):
    def test_read_only_is_a_recursive_read_only_mount(self):
        ro = labshare.server_script('/src', '/mnt', '/s.sock', True)
        rw = labshare.server_script('/src', '/mnt', '/s.sock', False)
        self.assertIn("mount -o rbind,ro=recursive '/src' '/mnt'", ro)
        self.assertIn("mount -o rbind '/src' '/mnt'", rw)
        self.assertNotIn('ro=recursive', rw)

    def test_whole_disk_hides_live_system_folders(self):
        cmd = labshare.server_command('x', {'scope': 'disk', 'readonly': True})
        self.assertEqual(cmd[:4], ['unshare', '--user', '--map-root-user', '--mount'])  # never root
        for sub in labshare.HIDDEN_IN_DISK:
            self.assertIn(f"/x.mnt/{sub}'", cmd[-1])
        self.assertNotIn('tmpfs', labshare.server_command('x', {'scope': 'home'})[-1])

    def test_paths_with_quotes_are_escaped(self):
        self.assertIn("'/a'\\''b'", labshare.server_script("/a'b", '/mnt', '/s.sock', False))

    def test_scopes(self):
        self.assertEqual(labshare.source_dir({'scope': 'disk'}), '/')
        self.assertEqual(labshare.source_dir({'scope': 'home'}), os.path.expanduser('~'))
        self.assertEqual(labshare.source_dir({'scope': 'folder', 'path': '/data/x'}), '/data/x')
        self.assertEqual(labshare.describe(None), 'Off')
        self.assertEqual(labshare.describe({'scope': 'disk', 'readonly': True}), 'Whole disk, read-only')


class Settings(unittest.TestCase):
    def test_save_keeps_other_settings_and_remove_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = labshare.SETTINGS
            labshare.SETTINGS = os.path.join(tmp, 'lab-manager', 'settings.json')
            try:
                os.makedirs(os.path.dirname(labshare.SETTINGS))
                with open(labshare.SETTINGS, 'w') as fh:
                    json.dump({'view': 'list'}, fh)
                labshare.save_config('VM1', {'scope': 'home', 'readonly': True})
                self.assertEqual(labshare.load_config('VM1'), {'scope': 'home', 'readonly': True})
                self.assertIsNone(labshare.load_config('VM2'))
                labshare.save_config('VM1', None)
                self.assertIsNone(labshare.load_config('VM1'))
                with open(labshare.SETTINGS) as fh:
                    self.assertEqual(json.load(fh)['view'], 'list')
            finally:
                labshare.SETTINGS = old

    def test_unknown_scope_counts_as_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = labshare.SETTINGS
            labshare.SETTINGS = os.path.join(tmp, 'settings.json')
            try:
                with open(labshare.SETTINGS, 'w') as fh:
                    json.dump({'share': {'VM1': {'scope': 'everything'}}}, fh)
                self.assertIsNone(labshare.load_config('VM1'))
            finally:
                labshare.SETTINGS = old


if __name__ == '__main__':
    unittest.main()
