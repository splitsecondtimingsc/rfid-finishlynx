"""Adding a read by hand through the web API: a coach's race time ("6:12.3")
is converted against the pane's gun on the server; clock times still work.
python -m unittest tests.test_add_read_api"""

import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.races import RaceManager

GUN = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)


class TestAddReadApi(unittest.TestCase):

    def setUp(self):
        self.mgr = RaceManager(on_change=lambda k: None, finish_readers={"FINISH"},
                               point_distances={"S1": 1609, "S2": 3218})
        self.key = self.mgr.create_race("Girls", event=2, gender="F", distance="5000")
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=self.mgr, offer_recent_starts=lambda k: None)
        self.c = web.app.test_client()

    def post(self, body):
        r = self.c.post(f"/api/races/{self.key}/add_read", data=json.dumps(body),
                        content_type="application/json")
        return r.status_code, r.get_json()

    def test_race_time_field_is_measured_from_the_gun(self):
        self.mgr.set_gun(self.key, GUN)
        code, d = self.post({"bib": "12", "point": "S1", "elapsed": "6:12.3"})
        self.assertEqual(code, 200, d)
        self.assertEqual(self.mgr.reads[d["read_id"]].ts, GUN + timedelta(seconds=372.3))
        self.assertFalse(self.mgr.reads[d["read_id"]].estimated)
        self.assertTrue(self.mgr.reads[d["read_id"]].manual)

    def test_clock_field_without_hours_is_race_time(self):
        self.mgr.set_gun(self.key, GUN)
        code, d = self.post({"bib": "12", "point": "S2", "clock": "12:30"})
        self.assertEqual(code, 200, d)
        self.assertEqual(self.mgr.reads[d["read_id"]].ts, GUN + timedelta(seconds=750))
        code, d = self.post({"bib": "13", "point": "S2", "clock": "1:02:03.5"})   # hours -> clock time
        self.assertEqual(code, 200, d)
        self.assertEqual(self.mgr.reads[d["read_id"]].ts.strftime("%H:%M:%S.%f")[:-3], "01:02:03.500")

    def test_race_time_needs_a_gun(self):
        code, d = self.post({"bib": "12", "point": "S1", "elapsed": "6:12"})
        self.assertEqual(code, 400)
        self.assertIn("no gun", d["error"])
        code, d = self.post({"bib": "12", "point": "S1", "clock": "09:06:12.000"})
        self.assertEqual(code, 200, d)

    def test_bad_inputs(self):
        self.mgr.set_gun(self.key, GUN)
        self.assertEqual(self.post({"bib": "", "point": "S1", "elapsed": "6:12"})[0], 400)
        self.assertEqual(self.post({"bib": "12", "point": "S1"})[0], 400)
        self.assertEqual(self.post({"bib": "12", "point": "S1", "elapsed": "six"})[0], 400)
        self.assertEqual(self.post({"bib": "12", "point": "S1", "elapsed": "6::12"})[0], 400)
        self.assertEqual(self.post({"bib": "12", "point": "FINISH", "elapsed": "19:30", "estimated": True})[0], 400)

    def test_parse_elapsed(self):
        self.assertEqual(web._parse_elapsed("372"), 372.0)
        self.assertEqual(web._parse_elapsed("6:12.3"), 372.3)
        self.assertEqual(web._parse_elapsed("1:02:03"), 3723.0)
        with self.assertRaises(ValueError):
            web._parse_elapsed("1:2:3:4")


if __name__ == "__main__":
    unittest.main()
