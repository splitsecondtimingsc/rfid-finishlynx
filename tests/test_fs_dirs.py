"""2026-10-03: /api/fs/dirs, the Lynx folder dialog's Browse… picker -
folder names only, the parent, Lynx files flagged, child joined server-side,
a missing path walks up to the nearest folder that exists; /api/fs/pick + /api/fs/save (the OS dialogs) only for a browser on the console PC."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from rfid_finishlynx import web


class FsDirsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, 'Meet', 'RFID'))
        os.makedirs(os.path.join(self.root, '.hidden'))
        open(os.path.join(self.root, 'Meet', 'lynx.evt'), 'w').close()
        open(os.path.join(self.root, 'notes.txt'), 'w').close()
        self.client = web.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, **q):
        return self.client.get('/api/fs/dirs', query_string=q).get_json()

    def test_lists_folders_only(self):
        d = self.get(path=self.root)
        self.assertEqual(d['dirs'], ['Meet'])                 # no files, no dot folders
        self.assertEqual(d['lynx_files'], [])
        self.assertEqual(d['parent'], os.path.dirname(os.path.abspath(self.root)))

    def test_child_is_joined_and_lynx_flagged(self):
        d = self.get(path=self.root, child='Meet')
        self.assertEqual(d['path'], os.path.join(os.path.abspath(self.root), 'Meet'))
        self.assertEqual(d['lynx_files'], ['lynx.evt'])
        self.assertTrue(d['has_rfid'])

    def test_missing_path_walks_up(self):
        d = self.get(path=os.path.join(self.root, 'Meet', 'gone', 'deeper'))
        self.assertEqual(d['path'], os.path.join(os.path.abspath(self.root), 'Meet'))


    def test_dialogs_refused_from_another_machine(self):
        for url in ('/api/fs/pick', '/api/fs/save'):
            r = self.client.post(url, json={'url': '/api/athletes'}, environ_base={'REMOTE_ADDR': '10.0.0.5'})
            self.assertEqual(r.status_code, 403, url)

    def test_pick_returns_the_picked_path_or_null(self):
        picked = os.path.join(self.root, 'Meet')
        for out, code, want in ((picked + '\n', 0, os.path.normpath(picked)), ('', 1, None)):
            done = subprocess.CompletedProcess([], code, stdout=out, stderr='')
            with mock.patch('subprocess.run', return_value=done):
                r = self.client.post('/api/fs/pick', json={'mode': 'dir', 'path': self.root})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()['path'], want)
        self.assertEqual(self.client.post('/api/fs/pick', json={'mode': 'rm'}).status_code, 400)

    def test_save_writes_the_export_where_picked(self):
        dest = os.path.join(self.root, 'out.json')
        done = subprocess.CompletedProcess([], 0, stdout=dest + '\n', stderr='')
        with mock.patch('subprocess.run', return_value=done):
            r = self.client.post('/api/fs/save', json={'url': '/api/athletes', 'name': 'a.json', 'ext': '.json'})
        self.assertEqual(r.get_json()['path'], os.path.normpath(dest))
        with open(dest) as f:
            self.assertIn('by_bib', f.read())
        cancelled = subprocess.CompletedProcess([], 1, stdout='', stderr='')
        with mock.patch('subprocess.run', return_value=cancelled):
            r = self.client.post('/api/fs/save', json={'url': '/api/athletes', 'name': 'a.json'})
        self.assertIsNone(r.get_json()['path'])
        for url in ('/etc/passwd', '/api/fs/dirs'):                 # only the console's own exports
            self.assertEqual(self.client.post('/api/fs/save', json={'url': url}).status_code, 400, url)


if __name__ == '__main__':
    unittest.main()
