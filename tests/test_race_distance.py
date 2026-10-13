"""A race's distance: typed as metres, miles or km, editable on the race
(owner, 2026-10-13: 2-mile JV races). python -m pytest tests/test_race_distance.py"""

import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.races import RaceManager, parse_distance

GUN = datetime(2026, 10, 13, 16, 0, 0)


class Parse(unittest.TestCase):

    def test_forms(self):
        self.assertEqual([parse_distance(x) for x in ("", "3200", "2 mi", "2mi", "2 miles", "1.5 mi",
                                                      "5k", "5 km", "3.2k", "1609 m")],
                         ["", "3200", "3219", "3219", "3219", "2414", "5000", "5000", "3200", "1609"])

    def test_nonsense(self):
        for x in ("two miles", "5 furlongs", "3", "99999"):
            with self.assertRaises(ValueError):
                parse_distance(x)


class OnTheRace(unittest.TestCase):

    def setUp(self):
        self.m = RaceManager(finish_readers={"FINISH"}, athlete_lookup=lambda b: {"gender": "M", "last_name": "X"})
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=self.m, meet_service=None, offer_recent_starts=lambda k: None)
        self.c = web.app.test_client()

    def post(self, path, body):
        r = self.c.post(path, data=json.dumps(body), content_type="application/json")
        return r.status_code, r.get_json()

    def test_a_2_mile_race_made_with_the_default_lets_its_finishers_in_once_set(self):
        code, d = self.post("/api/races/create", {"name": "JV boys", "event": 7, "gender": "M"})
        k = d["key"]
        self.m.set_gun(k, GUN)
        self.m.ingest("100", GUN + timedelta(minutes=10, seconds=30), "FINISH")   # 10:30 for 2 miles
        r = next(x for x in self.m.reads.values() if x.bib == "100")
        self.assertEqual(r.state, "held")                                           # too early for a 5K
        code, d = self.post(f"/api/races/{k}/scoring", {"scorers": 5, "displacers": 2, "distance": "2 mi"})
        self.assertEqual(code, 200, d)
        self.assertEqual(self.m.races[k].distance, "3219")
        self.assertEqual((r.state, r.race), ("assigned", k))

    def test_create_takes_miles_and_refuses_nonsense(self):
        code, d = self.post("/api/races/create", {"name": "JV", "event": 8, "gender": "F", "distance": "2 miles"})
        self.assertEqual(self.m.races[d["key"]].distance, "3219")
        code, d = self.post("/api/races/create", {"name": "JV", "event": 9, "gender": "F", "distance": "far"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
