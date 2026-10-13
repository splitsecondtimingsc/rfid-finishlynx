import csv
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.races import RaceManager

GUN = datetime(2026, 9, 26, 14, 5, 0)

ATHLETES = {
    "101": {"last_name": "Alvarez", "first_name": "K", "school": "North", "gender": "F"},
    "102": {"last_name": "Osei", "first_name": "M", "school": "North", "gender": "F"},
    "103": {"last_name": "Kirby", "first_name": "D", "school": "West", "gender": "F"},
    "201": {"last_name": "Tran", "first_name": "J", "school": "Central", "gender": "M"},
    "412": {"last_name": "Nakamura", "first_name": "R", "school": "Ridge", "gender": "F"},
}


def lookup(bib):
    return ATHLETES.get(bib)


def mgr(**kw):
    return RaceManager(athlete_lookup=lookup, chute_reader="CHUTE",
                       finish_readers={"FIN"},
                       point_labels={"S1": "Mile 1", "S2": "Mile 2"}, **kw)


class TestClassification(unittest.TestCase):

    def setUp(self):
        self.m = mgr()
        self.girls = self.m.create_race("Girls Varsity", event=3, gender="F")
        self.m.set_status(self.girls, "checkin")
        self.m.ingest("101", GUN - timedelta(minutes=5), "CHUTE")
        self.m.set_gun(self.girls, GUN)

    def test_chute_builds_roster(self):
        self.assertIn("101", self.m.races[self.girls].roster)

    def test_roster_read_assigned(self):
        out = self.m.ingest("101", GUN + timedelta(minutes=7), "S1")
        self.assertEqual(out["state"], "assigned")
        self.assertEqual(out["race"], self.girls)

    def test_unrostered_matching_gender_single_race(self):
        out = self.m.ingest("412", GUN + timedelta(minutes=7), "S1")
        self.assertEqual(out["state"], "assigned")

    def test_unknown_bib_held(self):
        out = self.m.ingest("999", GUN + timedelta(minutes=7), "S1")
        self.assertEqual(out["state"], "held")
        self.assertEqual(len(self.m.held_reads()), 1)

    def test_wrong_gender_held(self):
        out = self.m.ingest("201", GUN + timedelta(minutes=7), "S1")
        self.assertEqual(out["state"], "held")

    def test_implausibly_early_held(self):
        out = self.m.ingest("412", GUN + timedelta(seconds=30), "S1")
        self.assertEqual(out["state"], "held")

    def test_two_open_same_gender_races_held(self):
        # both races without a start list: the read is ambiguous, held
        third = self.m.create_race("Girls JV", event=6, gender="F")
        self.m.set_gun(third, GUN + timedelta(minutes=1))
        other = self.m.create_race("Girls Open", event=5, gender="F")
        self.m.set_gun(other, GUN + timedelta(minutes=1))
        out = self.m.ingest("412", GUN + timedelta(minutes=10), "S1")
        self.assertEqual(out["state"], "held")

    def test_unentered_runner_goes_to_the_open_race(self):
        # owner, 2026-10-13: Girls Varsity has a start list (check-ins) that
        # lacks 412; Girls Open has none - the open race takes her
        other = self.m.create_race("Girls Open", event=5, gender="F")
        self.m.set_gun(other, GUN + timedelta(minutes=1))
        out = self.m.ingest("412", GUN + timedelta(minutes=10), "S1")
        self.assertEqual((out["state"], out["race"]), ("assigned", other))
        # but roster still decides
        out = self.m.ingest("101", GUN + timedelta(minutes=10), "S1")
        self.assertEqual(out["race"], self.girls)

    def test_finish_promotes_held_within_window(self):
        # spectator-ish reads before the gun stay held
        self.m.ingest("999", GUN - timedelta(minutes=20), "S1")
        # her real splits (unknown bib - held)
        self.m.ingest("999", GUN + timedelta(minutes=7), "S1")
        self.m.ingest("999", GUN + timedelta(minutes=15), "S2")
        self.assertEqual(len(self.m.held_reads()), 3)
        # manual assign of her finish validates the in-window splits
        out = self.m.ingest("999", GUN + timedelta(minutes=24), "FIN")
        self.assertEqual(out["state"], "held")  # unknown gender, still held
        fin_id = self.m.held_reads()[-1]["id"]
        self.m.assign_read(fin_id, self.girls)
        held = self.m.held_reads()
        self.assertEqual(len(held), 1)  # only the pre-gun read remains
        res = self.m.results(self.girls)
        row = next(r for r in res["finishers"] if r["bib"] == "999")
        self.assertEqual(len(row["splits"]), 2)


class TestDerivedResults(unittest.TestCase):

    def setUp(self):
        self.m = mgr()
        self.race = self.m.create_race("Girls", event=3, gender="F")
        self.m.set_gun(self.race, GUN)
        for bib, fin_min in [("101", 20), ("102", 21), ("103", 22)]:
            self.m.ingest(bib, GUN + timedelta(minutes=7), "S1")
            self.m.ingest(bib, GUN + timedelta(minutes=fin_min), "FIN")

    def test_places_derived(self):
        res = self.m.results(self.race)
        self.assertEqual([r["bib"] for r in res["finishers"]], ["101", "102", "103"])
        self.assertEqual(res["finishers"][0]["place"], 1)
        self.assertAlmostEqual(res["finishers"][0]["finish"]["elapsed"], 1200.0)

    def test_gun_change_rederives_everything(self):
        self.m.set_gun(self.race, GUN - timedelta(minutes=2), "corrected")
        res = self.m.results(self.race)
        self.assertAlmostEqual(res["finishers"][0]["finish"]["elapsed"], 1320.0)

    def test_time_override_reorders(self):
        res = self.m.results(self.race)
        winner_read = res["finishers"][0]["finish"]["read_id"]
        self.m.override_time(winner_read, GUN + timedelta(minutes=23))
        res = self.m.results(self.race)
        self.assertEqual([r["bib"] for r in res["finishers"]], ["102", "103", "101"])
        # raw timestamp preserved
        self.assertIsNotNone(self.m.reads[winner_read].override_ts)
        self.assertEqual(self.m.reads[winner_read].ts, GUN + timedelta(minutes=20))

    def test_delete_read_rescores(self):
        res = self.m.results(self.race)
        self.m.ignore_read(res["finishers"][0]["finish"]["read_id"])
        res = self.m.results(self.race)
        self.assertEqual([r["bib"] for r in res["finishers"]], ["102", "103"])
        self.assertEqual([r["bib"] for r in res["on_course"]], ["101"])
        self.assertEqual(len(self.m.ignored_reads()), 1)

    def test_add_manual_read_places_in_order(self):
        self.m.add_manual_read(self.race, "412", GUN + timedelta(minutes=20, seconds=30), "FIN")
        res = self.m.results(self.race)
        self.assertEqual([r["bib"] for r in res["finishers"]],
                         ["101", "412", "102", "103"])

    def test_move_bib_rederives_against_new_gun(self):
        other = self.m.create_race("Girls JV", event=1, gender="F")
        self.m.set_gun(other, GUN - timedelta(minutes=26))
        self.m.move_bib("103", self.race, other)
        res = self.m.results(other)
        self.assertEqual(res["finishers"][0]["bib"], "103")
        self.assertAlmostEqual(res["finishers"][0]["finish"]["elapsed"],
                               (26 + 22) * 60.0)
        self.assertEqual(len(self.m.results(self.race)["finishers"]), 2)


class TestScoring(unittest.TestCase):

    def _race_with_finishers(self, teams, scorers=2, displacers=1):
        m = mgr()
        key = m.create_race("R", event=1, gender="F", scorers=scorers,
                            displacers=displacers)
        m.set_gun(key, GUN)
        for i, team in enumerate(teams):
            bib = f"9{i:02d}"
            ATHLETES[bib] = {"last_name": bib, "first_name": "",
                             "school": team, "gender": "F"}
            m.ingest(bib, GUN + timedelta(minutes=20, seconds=i), "FIN")
        return m, key

    def test_complete_teams_scored_incomplete_excluded(self):
        m, key = self._race_with_finishers(["A", "B", "A", "B", "C", "A", "B"])
        teams = m.results(key)["teams"]
        names = [t["team"] for t in teams]
        self.assertIn("A", names)
        self.assertIn("B", names)
        self.assertNotIn("C", names)  # only 1 finisher, needs 2
        # C's runner is excluded from scoring places entirely:
        a = next(t for t in teams if t["team"] == "A")
        self.assertEqual(a["places"], [1, 3])
        self.assertEqual(a["score"], 4)
        self.assertEqual(a["rank"], 1)

    def test_displacer_limit(self):
        # 4th A runner beyond scorers+displacers doesn't take a place
        m, key = self._race_with_finishers(["A", "A", "A", "A", "B", "B"])
        teams = m.results(key)["teams"]
        b = next(t for t in teams if t["team"] == "B")
        self.assertEqual(b["places"], [4, 5])


class TestLif(unittest.TestCase):

    def _lif_race(self):
        m = mgr()
        key = m.create_race("Girls Varsity 5000m", event=3, gender="F",
                            distance="5000")
        m.set_gun(key, GUN)
        m.ingest("101", GUN + timedelta(minutes=7, seconds=1), "S1")
        m.ingest("101", GUN + timedelta(minutes=14, seconds=30), "S2")
        m.ingest("101", GUN + timedelta(minutes=20), "FIN")
        return m, key

    def test_lif_uses_finishlynx_name_and_19_field_rows(self):
        """MeetPro's Splits Auto-Pull polls FinishLynx's file name
        (003-1-01.lif) and parses FinishLynx's row layout: 19 fields with
        every split in one quoted field. The 2026-09-17 meet showed nothing
        in MeetPro while the file was 3-1-1.lif with unquoted extra fields."""
        m, key = self._lif_race()
        with tempfile.TemporaryDirectory() as d:
            path = m.write_lif(key, d)
            self.assertEqual(os.path.basename(path), "003-1-01.lif")
            with open(path, newline="") as f:
                raw = f.read()
            self.assertTrue(os.path.exists(path + ".mirror"))
        self.assertIn("\r\n", raw)
        lines = raw.splitlines()
        header = lines[0].split(",")
        self.assertEqual(header[0], "3")
        self.assertEqual(header[3], "Girls Varsity 5000m")
        self.assertEqual(header[10], "14:05:00.000")
        row = next(csv.reader([lines[1]]))
        self.assertEqual(len(row), 19)
        self.assertEqual(row[0:2], ["1", "101"])
        self.assertEqual(row[3], "Alvarez")
        self.assertEqual(row[6], "20:00.000")
        # split points only - MeetPro stores a trailing finish as a split
        self.assertEqual(row[10], "7:01.000 (7:01.000),14:30.000 (7:29.000)")
        self.assertIn('"7:01.000 (7:01.000),14:30.000 (7:29.000)"', lines[1])   # one quoted field
        self.assertEqual(row[11], "14:25:00.000")          # finish time of day
        self.assertEqual(row[16], "20:00.000")
        self.assertEqual(row[18], "20:00.000")

    def test_lif_never_overwrites_finishlynx_own_file(self):
        m, key = self._lif_race()
        with tempfile.TemporaryDirectory() as d:
            path = m.write_lif(key, d)
            # FinishLynx saves the event: same name, its own content
            with open(path, "w", newline="") as f:
                f.write("3,1,1,Girls Varsity 5000m,,,,,,5000,14:05:00.0000\r\n"
                        "1,101,,Alvarez,K,North,19:59.870,,,,19:59.870 (19:59.870),14:25:00.000,,,,,,,19:59.869\r\n")
            m.ingest("102", GUN + timedelta(minutes=21), "FIN")
            self.assertIsNone(m.write_lif(key, d))
            with open(path) as f:
                self.assertIn("19:59.870", f.read())
            # our own unchanged file is simply left as is, and rewritten when it changes
            other = m.create_race("Boys", event=4, gender="M", distance="5000")
            m.set_gun(other, GUN)
            m.ingest("201", GUN + timedelta(minutes=18), "FIN")
            p2 = m.write_lif(other, d)
            self.assertEqual(m.write_lif(other, d), p2)
            m.ingest("201", GUN + timedelta(minutes=18, seconds=30), "FIN")   # re-read: same finisher, later time
            self.assertEqual(m.write_lif(other, d), p2)

    def test_missing_split_holds_later_splits_and_finish_only_omits_them(self):
        """MeetPro files splits by position and skips empty entries, so a
        Mile 2 without a Mile 1 would land under Mile 1 (seen in MeetPro
        2.3.137). Splits stop at the first missing point instead."""
        m, key = self._lif_race()
        m.ingest("102", GUN + timedelta(minutes=14, seconds=40), "S2")    # no Mile 1 for 102
        m.ingest("102", GUN + timedelta(minutes=21), "FIN")
        text = m.lif_content(key)
        rows = [next(csv.reader([l])) for l in text.splitlines()[1:]]
        self.assertEqual(rows[1][1], "102")
        self.assertEqual(rows[1][10], "")                                  # Mile 2 held back
        self.assertEqual(rows[0][10], "7:01.000 (7:01.000),14:30.000 (7:29.000)")
        only = m.lif_content(key, splits=False)
        rows = [next(csv.reader([l])) for l in only.splitlines()[1:]]
        self.assertEqual(rows[0][10], "")
        self.assertEqual(rows[1][10], "")
        self.assertEqual(len(rows[0]), 19)

    def test_save_lif_writes_the_operator_copy_and_reports_missing(self):
        m, key = self._lif_race()
        m.ingest("102", GUN + timedelta(minutes=14, seconds=40), "S2")
        m.ingest("102", GUN + timedelta(minutes=21), "FIN")
        with tempfile.TemporaryDirectory() as d:
            out = m.save_lif(key, os.path.join(d, "lif-exports"))
            self.assertEqual(os.path.basename(out["path"]), "003-1-01.lif")
            self.assertTrue(os.path.exists(out["path"]))
            self.assertFalse(os.path.exists(out["path"] + ".mirror"))
            self.assertEqual(out["finishers"], 2)
            self.assertEqual(out["missing"], [{"bib": "102", "last_name": "Osei", "missing": ["S1"]}])
            out = m.save_lif(key, os.path.join(d, "lif-exports"), splits=False)
            self.assertEqual(out["missing"], [])

    def test_finish_does_not_adopt_reads_later_than_itself(self):
        """A stale future-dated read (a station whose clock was hours off)
        became a 5:17:19 'split' on 2026-09-17 because a finish promoted
        every held read after the gun. Only reads up to the finish count."""
        m = mgr()
        key = m.create_race("Girls", event=3, gender="F")
        m.set_gun(key, GUN)
        m.ingest("999", GUN + timedelta(minutes=7), "S1")           # unknown bib: held
        m.ingest("999", GUN + timedelta(hours=5), "S2")             # stale, hours in the future
        m.ingest("999", GUN + timedelta(minutes=20), "FIN")
        m.assign_read(next(r["id"] for r in m.held_reads() if r["point"] == "FIN"), key)
        row = m.results(key)["finishers"][0]
        self.assertEqual([s["point"] for s in row["splits"]], ["S1"])
        self.assertEqual(len(m.held_reads()), 1)


class TestDeleteAndClose(unittest.TestCase):

    def _meet(self, state_path=None):
        m = mgr(state_path=state_path)
        girls = m.create_race("Girls", event=2, gender="F")
        boys = m.create_race("Boys", event=1, gender="M")
        m.set_gun(girls, GUN)
        m.ingest("101", GUN + timedelta(minutes=20), "FIN")
        m.ingest("999", GUN + timedelta(minutes=21), "FIN")   # unknown -> held
        return m, girls, boys

    def test_delete_race_releases_reads_to_held(self):
        m, girls, boys = self._meet()
        m.set_status(boys, "checkin")
        self.assertEqual(m.chute_target, boys)
        released = m.delete_race(girls)
        self.assertEqual(released, 1)
        self.assertNotIn(girls, m.races)
        self.assertEqual(m.order, [boys])
        held = m.held_reads()
        self.assertEqual({r["bib"] for r in held}, {"101", "999"})
        self.assertEqual(next(r for r in held if r["bib"] == "101")["reason"], "race deleted")
        # a held read can go straight into another race
        m.set_gun(boys, GUN)
        m.assign_read(next(r["id"] for r in held if r["bib"] == "101"), boys)
        self.assertEqual(m.results(boys)["finishers"][0]["bib"], "101")
        # deleting the chute target clears it
        m.delete_race(boys)
        self.assertIsNone(m.chute_target)
        self.assertRaises(KeyError, m.delete_race, boys)

    def test_close_meet_archives_then_clears(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "races.json")
            m, girls, boys = self._meet(state_path=path)
            archive = os.path.join(d, "closed", "20260915-1600")
            summary = m.close_meet(archive)
            self.assertEqual(summary["races"], 2)
            self.assertEqual(summary["reads"], 2)
            self.assertEqual(len(summary["archived"]), 2)
            with open(os.path.join(archive, "meet-results.csv")) as f:
                csv_text = f.read()
            self.assertIn("Girls,2,1,1,finished,1,101", csv_text)
            with open(os.path.join(archive, "races-state.json")) as f:
                self.assertEqual(len(json.load(f)["reads"]), 2)
            # everything gone, counters restart, and that is what a restart sees
            self.assertEqual(m.races, {})
            self.assertEqual(m.order, [])
            self.assertEqual(m.reads, {})
            self.assertEqual(m.held_reads(), [])
            self.assertEqual(m.create_race("Next meet", event=1), 1)
            m2 = mgr(state_path=path)
            self.assertEqual(list(m2.races), [1])
            self.assertEqual(m2.reads, {})
            out = m2.ingest("102", GUN, "FIN")
            self.assertEqual(m2.held_reads()[0]["id"], 1)

    def test_close_meet_without_archive_dir(self):
        m, girls, boys = self._meet()
        summary = m.close_meet(None)
        self.assertEqual(summary["archived"], [])
        self.assertEqual(m.races, {})

    def test_close_meet_archive_failure_leaves_meet_intact(self):
        with tempfile.TemporaryDirectory() as d:
            m, girls, boys = self._meet()
            blocker = os.path.join(d, "not-a-dir")
            with open(blocker, "w") as f:
                f.write("x")
            with self.assertRaises(OSError):
                m.close_meet(os.path.join(blocker, "archive"))
            self.assertEqual(len(m.races), 2)
            self.assertEqual(len(m.reads), 2)


class TestEstimateSplit(unittest.TestCase):
    """Missing-split estimates: pace over course distances + pack-mates."""

    S1, S2 = timedelta(minutes=6), timedelta(minutes=12, seconds=30)   # 1609 m / 3218 m
    FIN = timedelta(minutes=19, seconds=30)                            # 5000 m

    @staticmethod
    def _girl(bib):
        # every bib is a known girl so unrostered reads classify by gender
        return {"last_name": bib, "first_name": "", "school": "T" + bib[-1], "gender": "F"}

    def setUp(self):
        self.m = RaceManager(athlete_lookup=self._girl, finish_readers={"FIN"},
                             point_labels={"S1": "Mile 1", "S2": "Mile 2"},
                             point_distances={"S1": 1609, "S2": 3218})
        self.race = self.m.create_race("Girls", event=3, gender="F", distance="5000")
        self.m.set_gun(self.race, GUN)
        # a pack of five finishing 20 s apart, all with every split
        for i, bib in enumerate(["901", "902", "903", "904", "905"]):
            off = timedelta(seconds=20 * i)
            self.m.ingest(bib, GUN + self.S1 + off, "S1")
            self.m.ingest(bib, GUN + self.S2 + off, "S2")
            self.m.ingest(bib, GUN + self.FIN + off, "FIN")

    def test_interpolates_between_known_points_and_uses_pack(self):
        # 903-ish runner missing Mile 2, with Mile 1 and finish
        self.m.ingest("101", GUN + self.S1 + timedelta(seconds=45), "S1")
        self.m.ingest("101", GUN + self.FIN + timedelta(seconds=45), "FIN")
        est = self.m.estimate_split(self.race, "101", "S2")
        self.assertTrue(est["ok"], est)
        # pace: linear by distance between S1 (1609) and FIN (5000)
        frac = (3218 - 1609) / (5000 - 1609)
        pace_expected = 360 + 45 + (1170 - 360) * frac
        self.assertAlmostEqual(est["pace"]["elapsed"], pace_expected, places=3)
        # neighbours: pack-mates' S2-S1 segment (390 s) added to her Mile 1
        self.assertAlmostEqual(est["neighbours"]["elapsed"], 360 + 45 + 390, places=3)
        self.assertEqual(est["neighbours"]["count"], 4)
        self.assertIn("2 places", est["neighbours"]["detail"])
        blend = 0.6 * pace_expected + 0.4 * (360 + 45 + 390)
        self.assertAlmostEqual(est["suggested"]["elapsed"], blend, places=3)
        self.assertTrue(est["method"].startswith("blend"))

    def test_carries_pace_forward_when_nothing_after(self):
        # Only gun + Mile 1 known, nobody in the pack has a Mile 2 yet
        m = RaceManager(athlete_lookup=self._girl, finish_readers={"FIN"},
                        point_distances={"S1": 1609, "S2": 3218})
        race = m.create_race("Boys", event=1, gender="F", distance="5000")
        m.set_gun(race, GUN)
        m.ingest("201", GUN + timedelta(minutes=5), "S1")
        est = m.estimate_split(race, "201", "S2")
        self.assertTrue(est["ok"], est)
        self.assertEqual(est["method"], "pace")
        self.assertAlmostEqual(est["pace"]["elapsed"], 300 * 3218 / 1609, places=3)
        self.assertIsNone(est["neighbours"])

    def test_refusals(self):
        self.assertFalse(self.m.estimate_split(self.race, "101", "FIN")["ok"])       # never a finish
        self.assertFalse(self.m.estimate_split(self.race, "101", "S2")["ok"])        # no reads
        self.assertIn("already", self.m.estimate_split(self.race, "901", "S2")["reason"])

    def test_estimated_reads_are_flagged_not_evidence(self):
        self.m.ingest("101", GUN + self.S1 + timedelta(seconds=45), "S1")
        self.m.ingest("101", GUN + self.FIN + timedelta(seconds=45), "FIN")
        est = self.m.estimate_split(self.race, "101", "S2")
        rid = self.m.add_manual_read(self.race, "101", _p(est["suggested"]["clock"]), "S2",
                                     estimated=True, note="estimated: test")
        self.assertTrue(self.m.reads[rid].estimated)
        row = next(r for r in self.m.results(self.race)["finishers"] if r["bib"] == "101")
        self.assertTrue(next(s for s in row["splits"] if s["point"] == "S2")["estimated"])
        # CSV: tilde on the split, and the Estimated column names it
        rows = list(csv.reader(io.StringIO(self.m.results_csv([self.race]))))
        line = next(r for r in rows if len(r) > 6 and r[6] == "101")
        self.assertIn("~Mile 2", line[13])
        self.assertEqual(line[14], "Mile 2")
        # another runner missing Mile 2 next to 101 must not lean on 101's estimate
        self.m.ingest("102", GUN + self.S1 + timedelta(seconds=50), "S1")
        self.m.ingest("102", GUN + self.FIN + timedelta(seconds=50), "FIN")
        est2 = self.m.estimate_split(self.race, "102", "S2")
        # window of 2 places each side holds 903, 101, 904, 905 - 101's Mile 2
        # is an estimate, so only the three real pack-mates count
        self.assertEqual(est2["neighbours"]["count"], 3)
        self.assertAlmostEqual(est2["neighbours"]["elapsed"], 360 + 50 + 390, places=3)

    def test_estimated_flag_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "races.json")
            m = mgr(state_path=path)
            key = m.create_race("Girls", event=3, gender="F")
            m.set_gun(key, GUN)
            m.add_manual_read(key, "101", GUN + timedelta(minutes=6), "S1", estimated=True)
            m2 = mgr(state_path=path)
            self.assertTrue(next(iter(m2.reads.values())).estimated)


def _p(clock: str) -> datetime:
    return datetime.strptime(clock, "%Y-%m-%dT%H:%M:%S.%f")


class TestPersistence(unittest.TestCase):

    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "races.json")
            m = mgr(state_path=path)
            key = m.create_race("Girls", event=3, gender="F")
            m.set_status(key, "checkin")
            m.ingest("101", GUN - timedelta(minutes=5), "CHUTE")
            m.set_gun(key, GUN)
            m.ingest("101", GUN + timedelta(minutes=20), "FIN")

            m2 = mgr(state_path=path)
            self.assertIn("101", m2.races[key].roster)
            res = m2.results(key)
            self.assertEqual(res["finishers"][0]["bib"], "101")
            # id counters continue without collision
            out = m2.ingest("102", GUN + timedelta(minutes=21), "FIN")
            self.assertEqual(out["state"], "assigned")
            self.assertEqual(len(m2.results(key)["finishers"]), 2)


if __name__ == "__main__":
    unittest.main()
