"""/api/reads?detail=true attaches name / school / gender from the loaded
.ppl (the console's "Add athlete" pick-list). python -m unittest tests.test_reads_api_detail"""

import unittest
from types import SimpleNamespace

from rfid_finishlynx import web

READS = [
    {'bib': '35', 'time': '09:21:03.120', 'timestamp': '2026-09-19T09:21:03.120000', 'reader_id': 'FINISH'},
    {'bib': '12', 'time': '09:20:59.004', 'timestamp': '2026-09-19T09:20:59.004000', 'reader_id': 'FINISH'},
    {'bib': '999', 'time': '09:20:40.000', 'timestamp': '2026-09-19T09:20:40.000000', 'reader_id': 'S1'},
]


class TestReadsDetail(unittest.TestCase):

    def setUp(self):
        web._application = SimpleNamespace(get_reads=lambda: [dict(r) for r in READS])
        with web._athlete_lock:
            self._saved = web._athlete_data['by_bib']
            web._athlete_data['by_bib'] = {
                '35': {'first_name': 'Ada', 'last_name': 'Lovelace', 'school': 'North', 'gender': 'F'},
                '12': {'first_name': 'Bob', 'last_name': 'Beamon', 'school': 'South', 'gender': 'M'},
            }
        self.c = web.app.test_client()

    def tearDown(self):
        with web._athlete_lock:
            web._athlete_data['by_bib'] = self._saved

    def test_plain_call_is_unchanged(self):
        d = self.c.get('/api/reads').get_json()
        self.assertEqual([r['bib'] for r in d], ['35', '12', '999'])
        self.assertNotIn('athlete', d[0])

    def test_detail_adds_who_and_gender(self):
        d = self.c.get('/api/reads?detail=true').get_json()
        self.assertEqual(d[0]['athlete'], {'first_name': 'Ada', 'last_name': 'Lovelace', 'school': 'North',
                                           'gender': 'F', 'name': 'Ada Lovelace'})
        self.assertEqual(d[1]['athlete']['gender'], 'M')
        self.assertIsNone(d[2]['athlete'])                      # not in the roster

    def test_limit_and_order(self):
        d = self.c.get('/api/reads?detail=1&limit=2').get_json()
        self.assertEqual([r['bib'] for r in d], ['35', '12'])   # newest first
        d = self.c.get('/api/reads?sort=asc&limit=1').get_json()
        self.assertEqual([r['bib'] for r in d], ['999'])


if __name__ == "__main__":
    unittest.main()
