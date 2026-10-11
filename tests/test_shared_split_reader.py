"""One split station standing at two markers (Mile 1 and Mile 2 share a spot).

races.reader_points: {S1: [S1, S2]} makes the race manager hand out points
by pass number: an athlete's first pass at S1 is Mile 1, the second Mile 2.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

import yaml

from rfid_finishlynx import web
from rfid_finishlynx.config import Config, load_config
from rfid_finishlynx.races import RaceManager

GUN = datetime(2026, 10, 3, 10, 0, 0)

ATHLETES = {
    "101": {"last_name": "Alvarez", "first_name": "K", "school": "North", "gender": "F"},
    "102": {"last_name": "Osei", "first_name": "M", "school": "North", "gender": "F"},
    "103": {"last_name": "Kirby", "first_name": "D", "school": "West", "gender": "F"},
    "201": {"last_name": "Tran", "first_name": "J", "school": "Central", "gender": "M"},
    "202": {"last_name": "Diaz", "first_name": "P", "school": "Central", "gender": "M"},
    "412": {"last_name": "Nakamura", "first_name": "R", "school": "Ridge", "gender": "F"},
}


def mgr(**kw):
    kw.setdefault("reader_points", {"S1": ["S1", "S2"]})
    return RaceManager(athlete_lookup=ATHLETES.get, chute_reader="CHUTE",
                       finish_readers={"FIN"},
                       point_labels={"S1": "Mile 1", "S2": "Mile 2"},
                       point_distances={"S1": 1609, "S2": 3218}, **kw)


def m(minutes, seconds=0):
    return GUN + timedelta(minutes=minutes, seconds=seconds)


class TestPassNumbering(unittest.TestCase):

    def setUp(self):
        self.m = mgr()
        self.race = self.m.create_race("Girls Varsity", event=3, gender="F")
        self.m.set_status(self.race, "checkin")
        self.m.ingest("101", GUN - timedelta(minutes=5), "CHUTE")
        self.m.set_gun(self.race, GUN)

    def splits(self, bib):
        res = self.m.results(self.race)
        row = next(r for r in res["finishers"] + res["on_course"] if r["bib"] == bib)
        return [s["point"] for s in row["splits"]]

    def test_first_pass_is_mile_1_second_is_mile_2(self):
        a = self.m.ingest("101", m(6), "S1")
        b = self.m.ingest("101", m(13), "S1")
        self.assertEqual((a["state"], a["point"]), ("assigned", "S1"))
        self.assertEqual((b["state"], b["point"]), ("assigned", "S2"))
        self.assertEqual(self.splits("101"), ["S1", "S2"])
        # the raw reader is remembered, the point is derived
        reads = sorted(self.m.reads.values(), key=lambda r: r.ts)
        self.assertEqual([(r.reader, r.point) for r in reads if r.reader], [("S1", "S1"), ("S1", "S2")])

    def test_lingering_on_the_mat_is_one_pass(self):
        self.m.ingest("101", m(6), "S1")
        again = self.m.ingest("101", m(6, 40), "S1")      # 40 s later: same pass
        self.assertEqual(again["point"], "S1")
        real = self.m.ingest("101", m(13), "S1")
        self.assertEqual(real["point"], "S2")

    def test_third_pass_is_ignored(self):
        # one read per station per race (2026-10-09): a pass after the
        # station's points is ignored (it used to count as Mile 2 again)
        for t in (6, 13):
            self.m.ingest("101", m(t), "S1")
        third = self.m.ingest("101", m(20), "S1")
        self.assertEqual((third["state"], third["point"], third["crossing"]), ("ignored", "S2", "unexpected"))
        self.assertEqual(self.splits("101"), ["S1", "S2"])

    def test_athletes_are_counted_independently(self):
        self.m.ingest("101", m(6), "S1")
        self.assertEqual(self.m.ingest("102", m(6, 30), "S1")["point"], "S1")
        self.assertEqual(self.m.ingest("101", m(13), "S1")["point"], "S2")
        self.assertEqual(self.m.ingest("102", m(14), "S1")["point"], "S2")

    def test_other_readers_are_untouched(self):
        # S2 hands out no points; its second crossing is not a second Mile 2
        self.m.ingest("101", m(9), "S2")
        again = self.m.ingest("101", m(13), "S2")
        self.assertEqual(self.splits("101"), ["S2"])
        self.assertEqual((again["state"], again["point"]), ("ignored", "S2"))

    def test_unknown_bib_held_then_promoted_keeps_pass_order(self):
        # spectator loiter before the gun does not count as a pass
        self.m.ingest("999", GUN - timedelta(minutes=30), "S1")
        first = self.m.ingest("999", m(7), "S1")
        second = self.m.ingest("999", m(14), "S1")
        self.assertEqual((first["state"], first["point"]), ("held", "S1"))
        self.assertEqual((second["state"], second["point"]), ("held", "S2"))
        fin = self.m.ingest("999", m(21), "FIN")
        self.assertEqual(fin["state"], "held")           # unknown bib: operator assigns it
        self.m.assign_read(self.m.held_reads()[-1]["id"], self.race)
        self.assertEqual(self.splits("999"), ["S1", "S2"])
        held = [r for r in self.m.reads.values() if r.state == "held"]
        self.assertEqual([r.ts for r in held], [GUN - timedelta(minutes=30)])

    def test_ignoring_a_spurious_first_pass_shifts_the_rest(self):
        bogus = self.m.ingest("101", m(4, 30), "S1")     # plausible for Mile 1, but wrong
        self.m.ingest("101", m(8), "S1")
        self.m.ingest("101", m(15), "S1")
        self.assertEqual(self.splits("101"), ["S1", "S2"])        # the third pass is ignored
        bogus_id = next(r.id for r in self.m.reads.values() if r.ts == m(4, 30))
        self.m.ignore_read(bogus_id)
        self.assertEqual(self.splits("101"), ["S1", "S2"])
        real = {r.ts: r for r in self.m.reads.values()}
        self.assertEqual((real[m(8)].point, real[m(15)].point, real[m(15)].state), ("S1", "S2", "assigned"))

    def test_manual_assign_of_held_reads_relabels_inside_the_race(self):
        # a boy (no open boys' race) runs with the girls: both passes held
        first = self.m.ingest("201", m(7), "S1")
        second = self.m.ingest("201", m(14), "S1")
        self.assertEqual((first["state"], first["point"]), ("held", "S1"))
        self.assertEqual((second["state"], second["point"]), ("held", "S2"))
        ids = {r.ts: r.id for r in self.m.reads.values() if r.bib == "201"}
        # assigning the later pass first must not make it Mile 1
        self.m.assign_read(ids[m(14)], self.race)
        self.assertEqual(self.splits("201"), ["S2"])
        self.m.assign_read(ids[m(7)], self.race)
        self.assertEqual(self.splits("201"), ["S1", "S2"])

    def test_lif_carries_both_splits(self):
        self.m.ingest("101", m(6), "S1")
        self.m.ingest("101", m(13), "S1")
        self.m.ingest("101", m(20), "FIN")
        with tempfile.TemporaryDirectory() as d:
            path = self.m.write_lif(self.race, d)
            self.assertEqual(os.path.basename(path), "003-1-01.lif")
            with open(path) as f:
                line = f.read().splitlines()[1]
        # cumulative (lap) for Mile 1 and Mile 2; the finish is only the result time
        self.assertIn('"6:00.000 (6:00.000),13:00.000 (7:00.000)"', line)
        self.assertNotIn("20:00.000 (7:00.000)", line)


class TestSwitchingTheSetting(unittest.TestCase):

    def setUp(self):
        self.m = mgr(reader_points={})
        self.race = self.m.create_race("Boys", event=4, gender="M")
        self.m.set_gun(self.race, GUN)

    def points(self):
        res = self.m.results(self.race)
        return [s["point"] for s in res["on_course"][0]["splits"]]

    def test_turning_it_on_relabels_reads_already_heard(self):
        self.m.ingest("201", m(6), "S1")
        self.m.ingest("201", m(13), "S1")
        self.assertEqual(self.points(), ["S1"])                   # one read per station
        self.m.set_reader_points({"S1": ["S1", "S2"]})
        self.assertEqual(self.points(), ["S1", "S2"])
        # and off again: back to the reader id, one Mile 1
        self.m.set_reader_points({})
        self.assertEqual(self.points(), ["S1"])
        self.assertTrue(all(r.reader == "" for r in self.m.reads.values()))

    def test_single_point_renames_a_station(self):
        self.m.set_reader_points({"S1": ["S2"]})
        self.assertEqual(self.m.ingest("201", m(13), "S1")["point"], "S2")
        self.assertEqual(self.m.ingest("201", m(14, 30), "S1")["point"], "S2")

    def test_manual_reads_are_not_rebound_to_a_reader(self):
        self.m.add_manual_read(self.race, "201", m(6), "S1")
        self.m.set_reader_points({"S1": ["S1", "S2"]})
        r = next(iter(self.m.reads.values()))
        self.assertEqual((r.reader, r.point), ("", "S1"))

    def test_pass_window_is_adjustable(self):
        self.m.set_reader_points({"S1": ["S1", "S2"]}, pass_window_sec=20)
        self.m.ingest("201", m(6), "S1")
        self.assertEqual(self.m.ingest("201", m(6, 40), "S1")["point"], "S2")

    def test_state_file_round_trips_the_reader(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            a = mgr(state_path=path)
            k = a.create_race("Boys", event=4, gender="M")
            a.set_gun(k, GUN)
            a.ingest("201", m(6), "S1")
            a.ingest("201", m(13), "S1")
            b = mgr(state_path=path)
            reads = sorted(b.reads.values(), key=lambda r: r.ts)
            self.assertEqual([(r.reader, r.point) for r in reads], [("S1", "S1"), ("S1", "S2")])
            # a third pass after restart still counts from the restored ones
            third = b.ingest("201", m(20), "S1")
            self.assertEqual((third["point"], third["state"]), ("S2", "ignored"))


class TestLoopCourse(unittest.TestCase):
    """Two stations on a loop: runners cross the Mile 2 mat once before
    Mile 1. S2 = ["-", "S2"]: first crossing is not a split, second is."""

    def setUp(self):
        self.m = mgr(reader_points={"S2": ["-", "S2"]})
        self.race = self.m.create_race("Girls Varsity", event=3, gender="F")
        self.m.set_status(self.race, "checkin")
        self.m.ingest("101", GUN - timedelta(minutes=5), "CHUTE")
        self.m.set_gun(self.race, GUN)

    def row(self, bib):
        res = self.m.results(self.race)
        return next((r for r in res["finishers"] + res["on_course"] if r["bib"] == bib), None)

    def test_early_crossing_is_ignored_and_the_real_one_is_mile_2(self):
        early = self.m.ingest("101", m(3), "S2")
        self.assertEqual(early["state"], "ignored")
        self.assertIn("not a split", early["reason"])
        self.assertEqual(self.m.ingest("101", m(7), "S1")["point"], "S1")
        real = self.m.ingest("101", m(14), "S2")
        self.assertEqual((real["state"], real["point"]), ("assigned", "S2"))
        self.assertEqual([s["point"] for s in self.row("101")["splits"]], ["S1", "S2"])
        ignored = self.m.ignored_reads()
        self.assertEqual([(r["bib"], r["point"]) for r in ignored], [("101", "S2")])
        self.assertIn("loop course", ignored[0]["reason"])

    def test_aliases_for_the_skip_token(self):
        for alias in ("skip", "none", "ignore", "X"):
            self.m.set_reader_points({"S2": [alias, "S2"]})
            self.assertEqual(self.m.reader_points, {"S2": ["-", "S2"]})

    def test_restoring_a_skipped_read_makes_it_the_real_split(self):
        # the reader missed her first crossing, so the one it heard is Mile 2
        only = self.m.ingest("101", m(14), "S2")
        self.assertEqual(only["state"], "ignored")
        rid = self.m.ignored_reads()[0]["id"]
        self.m.assign_read(rid, self.race)
        self.assertEqual([s["point"] for s in self.row("101")["splits"]], ["S2"])
        self.assertEqual(self.m.ignored_reads(), [])

    def test_hand_ignoring_the_early_pass_does_not_shift_the_numbering_twice(self):
        self.m.ingest("101", m(3), "S2")
        self.m.ingest("101", m(14), "S2")
        self.assertEqual([s["point"] for s in self.row("101")["splits"]], ["S2"])
        # operator deletes the (already skipped) early read: the real one stays Mile 2
        early_id = next(r.id for r in self.m.reads.values() if r.ts == m(3))
        self.m.ignore_read(early_id)
        self.assertEqual([s["point"] for s in self.row("101")["splits"]], ["S2"])

    def test_switching_the_setting_off_returns_skipped_reads_to_held(self):
        self.m.ingest("101", m(3), "S2")
        self.m.set_reader_points({})
        held = self.m.held_reads()
        self.assertEqual([(r["bib"], r["point"]) for r in held], [("101", "S2")])
        self.assertEqual(self.m.ignored_reads(), [])

    def test_finish_mat_crossed_once_before_the_finish(self):
        self.m.set_reader_points({"FIN": ["-", "FIN"]})
        first = self.m.ingest("101", m(9), "FIN")
        self.assertEqual(first["state"], "ignored")
        self.assertIsNone(self.row("101"))                 # not a finisher yet
        self.m.ingest("101", m(7, 30), "S1")
        fin = self.m.ingest("101", m(19), "FIN")
        self.assertEqual((fin["state"], fin["point"]), ("assigned", "FIN"))
        r = self.row("101")
        self.assertEqual(r["place"], 1)
        self.assertEqual(r["finish"]["elapsed"], 19 * 60)

    # --- order check: a crossing after Mile 1 cannot be the early one

    def test_reader_missed_the_early_crossing_but_mile_1_proves_the_real_one(self):
        self.m.ingest("101", m(7), "S1")
        real = self.m.ingest("101", m(14), "S2")          # only S2 crossing heard
        self.assertEqual((real["state"], real["point"]), ("assigned", "S2"))
        self.assertEqual([s["point"] for s in self.row("101")["splits"]], ["S1", "S2"])
        self.assertEqual(self.m.ignored_reads(), [])

    def test_late_arriving_mile_1_read_unskips_the_real_crossing(self):
        skipped = self.m.ingest("101", m(14), "S2")       # radio delivered this first
        self.assertEqual(skipped["state"], "ignored")
        self.m.ingest("101", m(7), "S1")                   # Mile 1 arrives late
        self.assertEqual([s["point"] for s in self.row("101")["splits"]], ["S1", "S2"])
        self.assertEqual(self.m.ignored_reads(), [])

    def test_early_crossing_still_skipped_when_mile_1_is_known(self):
        self.m.ingest("101", m(3), "S2")
        self.m.ingest("101", m(7), "S1")
        self.m.ingest("101", m(14), "S2")
        self.assertEqual([s["point"] for s in self.row("101")["splits"]], ["S1", "S2"])
        self.assertEqual(len(self.m.ignored_reads()), 1)

    def test_finish_mat_order_check_uses_mile_2_not_mile_1(self):
        # loop crosses the finish mat between Mile 1 and Mile 2
        self.m.set_reader_points({"FIN": ["-", "FIN"]})
        self.m.ingest("101", m(7), "S1")
        early = self.m.ingest("101", m(9), "FIN")          # after Mile 1, before Mile 2: still early
        self.assertEqual(early["state"], "ignored")
        self.m.ingest("101", m(14), "S2")
        fin = self.m.ingest("101", m(19), "FIN")
        self.assertEqual(fin["point"], "FIN")
        self.assertEqual(self.row("101")["finish"]["elapsed"], 19 * 60)
        self.assertEqual(len(self.m.ignored_reads()), 1)

    def test_unknown_bib_skipped_then_promoted(self):
        self.m.ingest("999", m(3), "S2")                    # held-window skip
        self.m.ingest("999", m(7), "S1")
        self.m.ingest("999", m(14), "S2")
        self.assertEqual(len(self.m.held_reads()), 2)
        self.assertEqual(len(self.m.ignored_reads()), 1)
        self.m.ingest("999", m(21), "FIN")
        self.m.assign_read(self.m.held_reads()[-1]["id"], self.race)
        self.assertEqual([s["point"] for s in self.row("999")["splits"]], ["S1", "S2"])
        self.assertEqual(len(self.m.ignored_reads()), 1)


class TestMissingSplitReview(unittest.TestCase):
    """Finishers lacking a split that other runners have are flagged."""

    def setUp(self):
        self.m = mgr(reader_points={})
        self.race = self.m.create_race("Girls", event=3, gender="F")
        self.m.set_gun(self.race, GUN)
        for bib, s1, s2, fin in (("101", 6, 13, 20), ("102", 6.2, 13.5, 21), ("103", 6.5, None, 22)):
            self.m.ingest(bib, m(s1), "S1")
            if s2:
                self.m.ingest(bib, m(s2), "S2")
            self.m.ingest(bib, m(fin), "FIN")

    def test_finisher_missing_an_expected_split_is_listed(self):
        res = self.m.results(self.race)
        self.assertEqual(res["expected_points"], ["S1", "S2"])
        self.assertEqual(res["needs_review"], 1)
        by = {r["bib"]: r["missing"] for r in res["finishers"]}
        self.assertEqual(by, {"101": [], "102": [], "103": ["S2"]})

    def test_on_course_athletes_are_not_flagged(self):
        self.m.ingest("412", m(7), "S1")
        res = self.m.results(self.race)
        self.assertEqual(res["needs_review"], 1)
        self.assertEqual(next(r for r in res["on_course"] if r["bib"] == "412")["missing"], [])

    def test_a_point_nobody_has_is_not_expected(self):
        n = mgr(reader_points={})
        k = n.create_race("Boys", event=4, gender="M")
        n.set_gun(k, GUN)
        n.ingest("201", m(7), "S1")
        n.ingest("201", m(20), "FIN")
        res = n.results(k)
        self.assertEqual(res["expected_points"], ["S1"])
        self.assertEqual(res["needs_review"], 0)

    def test_hand_typed_splits_do_not_make_a_point_expected(self):
        n = mgr(reader_points={})
        k = n.create_race("Boys", event=4, gender="M")
        n.set_gun(k, GUN)
        n.ingest("201", m(20), "FIN")
        n.add_manual_read(k, "201", m(7), "S1")
        n.ingest("202", m(21), "FIN")
        self.assertEqual(n.results(k)["needs_review"], 0)

    def test_estimate_all_fills_the_gap_and_flags_it(self):
        out = self.m.estimate_missing(self.race)
        self.assertEqual(out["added"], [("103", "S2")])
        self.assertEqual(out["failed"], [])
        res = self.m.results(self.race)
        self.assertEqual(res["needs_review"], 0)
        row = next(r for r in res["finishers"] if r["bib"] == "103")
        s2 = next(s for s in row["splits"] if s["point"] == "S2")
        self.assertTrue(s2["estimated"])
        # between her Mile 1 (6:30) and finish (22:00), and not identical to a neighbour
        self.assertGreater(s2["elapsed"], 6.5 * 60)
        self.assertLess(s2["elapsed"], 22 * 60)

    def test_estimate_all_reports_what_it_could_not_do(self):
        n = mgr(reader_points={})
        k = n.create_race("Boys", event=4, gender="M")
        n.set_gun(k, GUN)
        n.ingest("201", m(7), "S1")
        n.ingest("201", m(20), "FIN")
        n.ingest("202", m(21), "FIN")                      # no split, no distance basis... pace from gun+finish
        out = n.estimate_missing(k)
        # 202 has gun + finish: pace interpolation works, so it is added
        self.assertEqual(out["added"], [("202", "S1")])

    def test_api_estimate_missing(self):
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(race_manager=self.m, offer_recent_starts=lambda k: None)
        c = web.app.test_client()
        r = c.post(f"/api/races/{self.race}/estimate_missing", data="{}", content_type="application/json")
        d = r.get_json()
        self.assertEqual(r.status_code, 200, d)
        self.assertEqual(d["added"], [{"bib": "103", "point": "S2"}])
        self.assertEqual(d["failed"], [])


class TestConfig(unittest.TestCase):

    def test_yaml_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.yaml")
            with open(path, "w") as f:
                yaml.dump({"races": {"reader_points": {"S1": ["S1", "S2"], "LAP1": "S2"},
                                     "pass_window_sec": 45}}, f)
            cfg = load_config(path)
        self.assertEqual(cfg.races.reader_points, {"S1": ["S1", "S2"], "LAP1": ["S2"]})
        self.assertEqual(cfg.races.pass_window_sec, 45.0)

    def test_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.races.reader_points, {})
        self.assertEqual(cfg.races.pass_window_sec, 90.0)


class TestConfigApi(unittest.TestCase):

    def setUp(self):
        self.mgr = mgr(reader_points={})
        self.cfg = Config()
        self.saved = []
        self.addCleanup(setattr, web, "_application", web._application)
        web._application = SimpleNamespace(config=self.cfg, race_manager=self.mgr,
                                           save_config=lambda: self.saved.append(1),
                                           is_running=False, finishlynx=None)
        self.c = web.app.test_client()

    def post(self, body):
        r = self.c.post("/api/config", data=json.dumps(body), content_type="application/json")
        return r.status_code, r.get_json()

    def test_select_both_points_for_one_station(self):
        code, d = self.post({"races": {"reader_points": {"S1": ["S1", "S2"]}, "pass_window_sec": 60}})
        self.assertEqual(code, 200, d)
        self.assertEqual(self.cfg.races.reader_points, {"S1": ["S1", "S2"]})
        self.assertEqual(self.cfg.races.pass_window_sec, 60.0)
        self.assertEqual(self.mgr.reader_points, {"S1": ["S1", "S2"]})
        self.assertEqual(self.mgr.pass_window_sec, 60.0)
        self.assertEqual(self.saved, [1])
        snap = self.mgr.snapshot()
        self.assertEqual(snap["reader_points"], {"S1": ["S1", "S2"]})
        self.assertEqual(snap["pass_window_sec"], 60.0)

    def test_back_to_station_id(self):
        self.post({"races": {"reader_points": {"S1": ["S1", "S2"]}}})
        code, _ = self.post({"races": {"reader_points": {}}})
        self.assertEqual(code, 200)
        self.assertEqual(self.mgr.reader_points, {})

    def test_unknown_point_is_rejected(self):
        code, d = self.post({"races": {"reader_points": {"S1": ["S1", "S9"]}}})
        self.assertEqual(code, 400)
        self.assertIn("S9", d["error"])
        self.assertEqual(self.mgr.reader_points, {})

    def test_loop_course_skip_is_accepted_but_not_alone(self):
        code, _ = self.post({"races": {"reader_points": {"S2": ["-", "S2"], "FINISH": ["skip", "FINISH"]}}})
        self.assertEqual(code, 200)
        self.assertEqual(self.mgr.reader_points, {"S2": ["-", "S2"], "FINISH": ["-", "FINISH"]})
        code, d = self.post({"races": {"reader_points": {"S2": ["-"]}}})
        self.assertEqual(code, 400)
        self.assertIn("real split point", d["error"])

    def test_get_config_reports_it(self):
        self.cfg.races.reader_points = {"S1": ["S1", "S2"]}
        d = self.c.get("/api/config").get_json()
        self.assertEqual(d["races"]["reader_points"], {"S1": ["S1", "S2"]})


if __name__ == "__main__":
    unittest.main()
