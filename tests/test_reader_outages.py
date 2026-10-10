"""Finish-reader dropouts are recorded for the console's alarm and the
pane's outage note. python -m unittest tests.test_reader_outages"""

import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.app import Application


def bare():
    app = Application.__new__(Application)
    app._reader_outages = []
    app._reader_was_connected = None
    return app


class TestOutages(unittest.TestCase):

    def test_down_and_up_make_one_outage_with_duration(self):
        app = bare()
        t0 = datetime(2026, 9, 17, 18, 32, 22)
        app._note_reader_state(True, t0 - timedelta(minutes=5))
        app._note_reader_state(False, t0)
        app._note_reader_state(False, t0 + timedelta(seconds=2))       # still down: no new entry
        self.assertEqual(len(app._reader_outages), 1)
        self.assertIsNone(app._reader_outages[0]["end"])
        app._note_reader_state(True, t0 + timedelta(seconds=24))
        out = app.reader_outages()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["start"], t0.isoformat())
        self.assertEqual(out[0]["seconds"], 24.0)

    def test_open_outage_reports_running_duration(self):
        app = bare()
        app._note_reader_state(False, datetime.now() - timedelta(seconds=30))
        self.assertGreaterEqual(app.reader_outages()[0]["seconds"], 30)
        self.assertIsNone(app.reader_outages()[0]["end"])

    def test_list_is_capped(self):
        app = bare()
        t = datetime(2026, 9, 17, 12, 0, 0)
        for i in range(120):
            app._note_reader_state(False, t + timedelta(seconds=2 * i))
            app._note_reader_state(True, t + timedelta(seconds=2 * i + 1))
        self.assertEqual(len(app._reader_outages), Application.MAX_OUTAGES)
        self.assertEqual(app._reader_outages[-1]["start"], (t + timedelta(seconds=238)).isoformat())


if __name__ == "__main__":
    unittest.main()
