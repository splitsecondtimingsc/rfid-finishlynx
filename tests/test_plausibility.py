"""Earliest-plausible time per point, and per-race gender mode.

A read at Mile 1 two minutes after the gun is a spectator brushing the mat,
not a split: each point has a floor = its course distance at the fastest
pace anyone in the race could run (default 4:00/mile). Races are Girls,
Boys, Mixed (X: takes any athlete) or blank (roster only).
python -m unittest tests.test_plausibility
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.races import RaceManager

GUN = datetime(2026, 10, 10, 9, 0, 0)

ATHLETES = {
    "101": {"last_name": "Alvarez", "first_name": "K", "school": "North", "gender": "F"},
    "102": {"last_name": "Osei", "first_name": "M", "school": "North", "gender": "F"},
    "201": {"last_name": "Tran", "first_name": "J", "school": "Central", "gender": "M"},
    "202": {"last_name": "Diaz", "first_name": "P", "school": "Central", "gender": "M"},
    "301": {"last_name": "Roe", "first_name": "S", "school": "West", "gender": ""},
}


def mgr(**kw):
    return RaceManager(athlete_lookup=ATHLETES.get, chute_reader="CHUTE",
                       finish_readers={"FIN"},
                       point_labels={"S1": "Mile 1", "S2": "Mile 2"},
                       point_distances={"S1": 1609.344, "S2": 3218.688}, **kw)


def m(minutes, seconds=0):
    return GUN + timedelta(minutes=minutes, seconds=seconds)


class TestEarliestPlausible(unittest.TestCase):

    def setUp(self):
        self.m = mgr()
        self.race = self.m.create_race("Girls Varsity", event=3, gender="F", distance="5000")
        self.m.set_status(self.race, "checkin")
        self.m.ingest("101", GUN - timedelta(minutes=5), "CHUTE")
        self.m.set_gun(self.race, GUN)

    def test_floors_follow_distance_at_the_default_pace(self):
        f = self.m.floors(self.m.races[self.race])
        self.assertAlmostEqual(f["S1"], 240.0, places=3)
        self.assertAlmostEqual(f["S2"], 480.0, places=3)
        self.assertAlmostEqual(f["FIN"], 5000 / 1609.344 * 240, places=3)   # 12:26

    def test_rostered_read_too_early_for_mile_1_is_held(self):
        out = self.m.ingest("101", m(2, 10), "S1")
        self.assertEqual(out["state"], "held")
        self.assertIn("too early for Mile 1", out["reason"])
        self.assertIn("2:10 after the gun", out["reason"])
        self.assertIn("fastest allowed 4:00", out["reason"])
        self.assertTrue(self.m.reads[1].early if 1 in self.m.reads else True)
        ok = self.m.ingest("101", m(4, 30), "S1")
        self.assertEqual(ok["state"], "assigned")

    def test_mile_2_before_eight_minutes_is_held(self):
        self.assertEqual(self.m.ingest("101", m(7), "S2")["state"], "held")
        self.assertEqual(self.m.ingest("101", m(8, 30), "S2")["state"], "assigned")

    def test_finish_floor_uses_the_race_distance(self):
        self.assertEqual(self.m.ingest("101", m(10), "FIN")["state"], "held")
        self.assertEqual(self.m.ingest("101", m(13), "FIN")["state"], "assigned")
        short = self.m.create_race("Girls 3K", event=4, gender="F", distance="3000")
        self.m.set_gun(short, GUN)
        self.m.set_status(self.race, "final")
        self.assertEqual(self.m.ingest("102", m(9), "FIN")["state"], "assigned")   # floor 7:27

    def test_unrostered_read_too_early_is_held_with_the_reason(self):
        out = self.m.ingest("102", m(3), "S1")
        self.assertEqual(out["state"], "held")
        self.assertIn("too early for Mile 1", out["reason"])

    def test_early_reads_are_not_promoted_by_a_finish(self):
        self.m.ingest("101", m(1), "S1")                    # brush at the start
        self.m.ingest("101", m(6), "S1")
        self.m.ingest("101", m(20), "FIN")
        res = self.m.results(self.race)
        row = next(r for r in res["finishers"] if r["bib"] == "101")
        self.assertEqual([s["point"] for s in row["splits"]], ["S1"])
        held = self.m.held_reads()
        self.assertEqual(len(held), 1)
        self.assertIn("too early", held[0]["reason"])

    def test_manual_reads_are_the_operators_call(self):
        rid = self.m.add_manual_read(self.race, "101", m(2), "S1")
        self.assertEqual(self.m.reads[rid].state, "assigned")

    def test_race_can_override_the_pace_and_held_reads_are_rechecked(self):
        self.m.set_scoring(self.race, 5, 2, fastest_pace=300)          # 5:00/mile: Mile 1 >= 5:00
        self.assertEqual(self.m.ingest("101", m(4, 30), "S1")["state"], "held")
        self.m.set_scoring(self.race, 5, 2, fastest_pace=240)          # back to 4:00: now fine
        self.assertEqual(self.m.held_reads(), [])
        res = self.m.results(self.race)
        self.assertEqual([s["point"] for s in res["on_course"][0]["splits"]], ["S1"])

    def test_pace_bounds(self):
        with self.assertRaises(ValueError):
            self.m.set_scoring(self.race, 5, 2, fastest_pace=60)
        self.m.set_scoring(self.race, 5, 2, fastest_pace=None, keep_pace=False)
        self.assertIsNone(self.m.races[self.race].fastest_pace)

    def test_two_open_races_the_newer_one_too_recent(self):
        # 102 (unrostered girl) read 7 min after race A's gun, 1 min after race B's
        other = self.m.create_race("Girls JV", event=5, gender="F")
        self.m.set_gun(other, m(6))
        out = self.m.ingest("102", m(7), "S1")
        self.assertEqual(out["race"], self.race)

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            a = mgr(state_path=path)
            k = a.create_race("Girls", event=3, gender="F")
            a.set_gun(k, GUN)
            a.set_scoring(k, 5, 2, fastest_pace=300)
            a.ingest("102", m(4, 30), "S1")                # held early at 5:00/mile
            b = mgr(state_path=path)
            self.assertEqual(b.races[k].fastest_pace, 300)
            self.assertTrue(next(iter(b.reads.values())).early)


class TestPassNumberedReadersWithFloor(unittest.TestCase):

    def test_brush_at_the_start_does_not_eat_a_pass(self):
        n = mgr(reader_points={"S1": ["S1", "S2"]})
        k = n.create_race("Girls", event=3, gender="F")
        n.set_status(k, "checkin")
        n.ingest("101", GUN - timedelta(minutes=5), "CHUTE")
        n.set_gun(k, GUN)
        self.assertEqual(n.ingest("101", m(0, 30), "S1")["state"], "held")     # brush
        self.assertEqual(n.ingest("101", m(6), "S1")["point"], "S1")
        self.assertEqual(n.ingest("101", m(13), "S1")["point"], "S2")

    def test_loop_course_early_crossing_is_still_the_skip_slot(self):
        n = mgr(reader_points={"S2": ["-", "S2"]})
        k = n.create_race("Girls", event=3, gender="F")
        n.set_status(k, "checkin")
        n.ingest("101", GUN - timedelta(minutes=5), "CHUTE")
        n.set_gun(k, GUN)
        early = n.ingest("101", m(3), "S2")
        self.assertEqual(early["state"], "ignored")
        self.assertIn("not a split", early["reason"])
        self.assertEqual(n.ingest("101", m(7), "S1")["point"], "S1")
        self.assertEqual(n.ingest("101", m(14), "S2")["point"], "S2")


class TestGenderMode(unittest.TestCase):

    def setUp(self):
        self.m = mgr()

    def open_race(self, name, gender, gun=GUN):
        k = self.m.create_race(name, event=len(self.m.races) + 1, gender=gender)
        self.m.set_gun(k, gun)
        return k

    def test_mixed_race_takes_either_gender_and_unknown_gender(self):
        k = self.open_race("Open 5K", "X")
        for bib in ("101", "201", "301"):
            self.assertEqual(self.m.ingest(bib, m(7), "S1")["race"], k, bib)
        # a bib the meet file does not know is still held
        self.assertEqual(self.m.ingest("999", m(7), "S1")["state"], "held")

    def test_blank_gender_race_takes_nobody_by_gender(self):
        self.open_race("Unknown", "")
        out = self.m.ingest("101", m(7), "S1")
        self.assertEqual(out["state"], "held")
        self.assertIn("mixed races take anyone", out["reason"])

    def test_girls_race_still_rejects_boys(self):
        self.open_race("Girls", "F")
        self.assertEqual(self.m.ingest("201", m(7), "S1")["state"], "held")
        self.assertEqual(self.m.ingest("101", m(7), "S1")["state"], "assigned")

    def test_girls_and_mixed_open_together_is_ambiguous_for_a_girl(self):
        self.open_race("Girls", "F")
        self.open_race("Open", "X")
        out = self.m.ingest("101", m(7), "S1")
        self.assertEqual(out["state"], "held")
        self.assertIn("ambiguous", out["reason"])
        self.assertEqual(self.m.ingest("201", m(7), "S1")["state"], "assigned")   # only Open takes him

    def test_switching_a_race_to_mixed_pulls_in_held_reads(self):
        k = self.open_race("Girls", "F")
        self.assertEqual(self.m.ingest("201", m(7), "S1")["state"], "held")
        self.m.set_scoring(k, 5, 2, gender="X")
        self.assertEqual(self.m.held_reads(), [])
        self.assertEqual(self.m.reads[1].race, k)
        self.assertEqual(self.m.races[k].gender, "X")


class TestScoringApi(unittest.TestCase):

    def setUp(self):
        self.m = mgr()
        self.k = self.m.create_race("Girls", event=3, gender="F")
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=self.m, offer_recent_starts=lambda k: None)
        self.c = web.app.test_client()

    def post(self, body):
        r = self.c.post(f"/api/races/{self.k}/scoring", data=json.dumps(body),
                        content_type="application/json")
        return r.status_code, r.get_json()

    def test_gender_and_pace_from_the_dialog(self):
        code, d = self.post({"scorers": 5, "displacers": 2, "gender": "X", "fastest_pace": "4:30"})
        self.assertEqual(code, 200, d)
        race = self.m.races[self.k]
        self.assertEqual(race.gender, "X")
        self.assertEqual(race.fastest_pace, 270.0)
        snap = self.m.snapshot()["races"][str(self.k)]
        self.assertEqual(snap["fastest_pace"], 270.0)
        self.assertAlmostEqual(snap["floors"]["S1"], 270.0, places=3)
        code, _ = self.post({"scorers": 5, "displacers": 2, "fastest_pace": ""})
        self.assertEqual(code, 200)
        self.assertIsNone(self.m.races[self.k].fastest_pace)

    def test_save_lif_endpoint(self):
        self.m.set_gun(self.k, GUN)
        self.m.ingest("101", m(7), "S1")
        self.m.ingest("101", m(20), "FIN")
        with tempfile.TemporaryDirectory() as d:
            # saves into <meet directory>\lif-exports, beside FinishLynx's files
            web._application.config = SimpleNamespace(event_directory=d)
            r = self.c.post(f"/api/races/{self.k}/save_lif", data=json.dumps({"splits": False}),
                            content_type="application/json")
            out = r.get_json()
            self.assertEqual(r.status_code, 200, out)
            self.assertEqual(os.path.dirname(out["path"]), os.path.join(d, "lif-exports"))
            self.assertTrue(os.path.exists(out["path"]))
            self.assertFalse(out["splits"])
            # no meet directory: the middleware's own folder
            web._application.config = SimpleNamespace(event_directory="")
            r = self.c.post(f"/api/races/{self.k}/save_lif", data=json.dumps({"splits": True}),
                            content_type="application/json")
            self.assertEqual(os.path.dirname(r.get_json()["path"]), os.path.join(os.getcwd(), "lif-exports"))

    def test_bad_values_are_rejected(self):
        self.assertEqual(self.post({"scorers": 5, "displacers": 2, "fastest_pace": "fast"})[0], 400)
        self.assertEqual(self.post({"scorers": 5, "displacers": 2, "fastest_pace": "0:30"})[0], 400)
        self.assertEqual(self.post({"scorers": 5, "displacers": 2, "gender": "Q"})[0], 400)


if __name__ == "__main__":
    unittest.main()
