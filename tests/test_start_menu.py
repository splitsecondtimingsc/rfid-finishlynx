"""The race card's Start ▾ (2026-10-12, owner): Arm for Space, "Race is at…",
and resetting one race's film offset from its pane.
python -m unittest tests.test_start_menu"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.film_offset import FilmOffset, parse_tod
from rfid_finishlynx.races import RaceManager

GUN = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
FL_START = "16:46:49.0515"
BASE = -0.028


def read_ts(race_secs):
    return datetime(2026, 9, 18) + timedelta(seconds=parse_tod(FL_START) + race_secs)


class TestArmForSpace(unittest.TestCase):

    def setUp(self):
        self.m = RaceManager(finish_readers={"FINISH"})
        self.k = self.m.create_race("Girls", event=1, gender="F", distance="5000")

    def test_armed_race_does_not_take_a_start_signal(self):
        self.m.arm_space(self.k, True)
        self.m.add_gun_candidate(self.k, GUN, "camera start")
        race = self.m.races[self.k]
        self.assertIsNone(race.gun)                       # waits for Space
        self.assertEqual([c["source"] for c in race.gun_candidates], ["camera start"])
        self.assertTrue(race.space_armed)

    def test_space_press_sets_the_gun_and_disarms(self):
        self.m.arm_space(self.k, True)
        self.m.set_gun(self.k, GUN, "Space")
        race = self.m.races[self.k]
        self.assertEqual((race.gun, race.gun_source, race.status, race.space_armed), (GUN, "Space", "racing", False))

    def test_disarmed_race_takes_the_start_as_before(self):
        self.m.arm_space(self.k, True)
        self.m.arm_space(self.k, False)
        self.m.add_gun_candidate(self.k, GUN, "camera start")
        self.assertEqual(self.m.races[self.k].gun, GUN)

    def test_armed_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "races-state.json")
            m = RaceManager(finish_readers={"FINISH"}, state_path=path)
            k = m.create_race("Boys", event=2, gender="M")
            m.arm_space(k, True)
            m._save()
            back = RaceManager(finish_readers={"FINISH"}, state_path=path)
            self.assertTrue(back.races[k].space_armed)
            self.assertTrue(back.snapshot()["races"][str(k)]["space_armed"])


class TestStartApi(unittest.TestCase):

    def setUp(self):
        self.m = RaceManager(finish_readers={"FINISH"})
        self.k = self.m.create_race("Girls", event=1, gender="F", distance="5000")
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=self.m, offer_recent_starts=lambda k: None)
        self.c = web.app.test_client()

    def post(self, path, body):
        r = self.c.post(path, data=json.dumps(body), content_type="application/json")
        return r.status_code, r.get_json()

    def test_race_is_at(self):
        before = datetime.now()
        code, d = self.post(f"/api/races/{self.k}/gun", {"race_time": "5:00", "source": "race is at 5:00"})
        self.assertEqual(code, 200, d)
        race = self.m.races[self.k]
        self.assertAlmostEqual((before - race.gun).total_seconds(), 300, delta=2)
        self.assertEqual(race.gun_source, "race is at 5:00")
        self.assertEqual(self.post(f"/api/races/{self.k}/gun", {"race_time": "five"})[0], 400)

    def test_space_press_lag_is_taken_off(self):
        code, d = self.post(f"/api/races/{self.k}/gun", {"clock": "now", "source": "Space", "lag_ms": 400})
        self.assertEqual(code, 200, d)
        lag = (datetime.now() - self.m.races[self.k].gun).total_seconds()
        self.assertGreaterEqual(lag, 0.39)
        self.assertLess(lag, 2.0)

    def test_arm_endpoint(self):
        self.assertEqual(self.post(f"/api/races/{self.k}/arm", {"on": True})[0], 200)
        self.assertTrue(self.m.races[self.k].space_armed)
        self.assertEqual(self.post(f"/api/races/{self.k}/arm", {"on": False})[0], 200)
        self.assertFalse(self.m.races[self.k].space_armed)
        self.assertEqual(self.post("/api/races/999/arm", {"on": True})[0], 404)


class TestResetOneRace(unittest.TestCase):

    def _learn(self, fo, race, evaluated):
        reads = {b: (read_ts(t), 0) for b, t in {"1": 700.0, "2": 710.0, "3": 720.0, "4": 730.0, "5": 740.0}.items()}
        rows = {"1": round(700.0 - BASE, 2), "2": round(710.0 - BASE, 2), "3": round(720.0 - BASE, 2)}
        rows.update({b: round(t - v, 2) for b, (t, v) in evaluated.items()})
        fo.learn(race, parse_tod(FL_START), rows, reads)
        return rows, reads

    def test_one_race_goes_back_to_zero_and_relearns_only_new_evaluations(self):
        fo = FilmOffset()
        rows1, reads = self._learn(fo, 1, {"4": (730.0, 0.60), "5": (740.0, 0.70)})
        self._learn(fo, 2, {"4": (730.0, 0.50), "5": (740.0, 0.50)})
        other = fo.learned(2)
        fo.reset_race(1)
        self.assertIsNone(fo.learned(1))
        self.assertEqual(fo.correction(0, 1), 0.0)
        self.assertAlmostEqual(fo.learned(2), other)          # race 2 keeps its own
        fo.learn(1, parse_tod(FL_START), rows1, reads)         # same file again: nothing
        self.assertIsNone(fo.learned(1))
        rows1["4"] = round(730.0 - 0.66, 2)                    # re-evaluated after the reset
        fo.learn(1, parse_tod(FL_START), rows1, reads)
        self.assertAlmostEqual(fo.learned(1), 0.66 - BASE, delta=0.012)
        # a later full reset keeps the earlier one-race freeze
        fo.reset()
        rows1["4"] = round(730.0 - 0.60, 2)
        self.assertIn("1:5", fo.frozen)

    def test_api_resets_one_race(self):
        m = RaceManager(finish_readers={"FINISH"})
        k = m.create_race("Girls", event=1, gender="F")
        self._learn(m.film, k, {"4": (730.0, 0.60), "5": (740.0, 0.70)})
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=m, offer_recent_starts=lambda k: None)
        c = web.app.test_client()
        r = c.post("/api/film_offset", data=json.dumps({"reset_race": k}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(m.film.learned(k))
        r = c.post("/api/film_offset", data=json.dumps({"reset_race": 999}), content_type="application/json")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
