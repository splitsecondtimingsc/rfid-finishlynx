"""Fixes from the Crescent field test (owner's scenarios, 2026-10-13).
python -m pytest tests/test_fieldtest_fixes.py"""

import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.races import RaceManager

GUN = datetime(2026, 10, 8, 17, 46, 37)
ATH = {"1": {"gender": "M", "last_name": "A"}, "2": {"gender": "F", "last_name": "B"}}


class Starts(unittest.TestCase):

    def setUp(self):
        self.m = RaceManager(finish_readers={"FINISH"}, athlete_lookup=lambda b: ATH.get(b, {}))
        self.girls = self.m.create_race("Girls", event=1, gender="F")
        self.boys = self.m.create_race("Boys", event=2, gender="M")

    def where(self, bib):
        r = max((x for x in self.m.reads.values() if x.bib == bib), key=lambda x: x.id)
        return r.race if r.state == "assigned" else r.reason

    def test_the_wrong_card_started_then_the_right_one(self):
        """The Boys ran but the Girls card was started: the Boys' reads are
        held, and flow into the Boys card once it gets its start."""
        self.m.set_gun(self.girls, GUN)
        self.m.ingest("1", GUN + timedelta(minutes=17), "FINISH")
        self.assertIn("no open M race", self.where("1"))
        self.m.set_gun(self.boys, GUN)
        self.assertEqual(self.where("1"), self.boys)

    def test_a_start_signal_pulls_held_reads_in_too(self):
        self.m.ingest("1", GUN + timedelta(minutes=17), "FINISH")
        self.m.add_gun_candidate(self.boys, GUN, "camera start")
        self.assertEqual(self.where("1"), self.boys)

    def test_reads_before_the_start_stay_held(self):
        self.m.ingest("1", GUN - timedelta(minutes=10), "FINISH")       # a warm-up crossing
        self.m.set_gun(self.boys, GUN)
        self.assertNotEqual(self.where("1"), self.boys)

    def test_an_unknown_bib_says_so(self):
        self.m.set_gun(self.boys, GUN)
        self.m.ingest("9999", GUN + timedelta(minutes=17), "FINISH")
        self.assertIn("unknown bib", self.where("9999"))


class TypedRaceTime(unittest.TestCase):
    """Owner, 2026-10-13: a finish typed as a race time stays as typed when
    the start moves; one typed as a clock time stays that clock time."""

    def setUp(self):
        self.m = RaceManager(finish_readers={"FINISH"}, athlete_lookup=lambda b: ATH.get(b, {}))
        self.k = self.m.create_race("Boys", event=2, gender="M")
        self.m.set_gun(self.k, GUN)

    def el(self, bib):
        return round(next(r["finish"]["elapsed"] for r in self.m.results(self.k)["finishers"] if r["bib"] == bib), 3)

    def test_race_time_stays(self):
        self.m.set_finish_time(self.k, "1", GUN + timedelta(seconds=1799), elapsed=1799.0)   # typed, no read
        self.m.races[self.k].roster.add("2")
        self.m.ingest("2", GUN + timedelta(seconds=1700), "FINISH")
        self.m.set_finish_time(self.k, "2", GUN + timedelta(seconds=1750), elapsed=1750.0)   # a read retimed
        self.m.set_gun(self.k, GUN + timedelta(seconds=2.5))
        self.assertEqual((self.el("1"), self.el("2")), (1799.0, 1750.0))

    def test_clock_time_moves_with_the_start(self):
        self.m.set_finish_time(self.k, "1", GUN + timedelta(seconds=1799))                   # typed as a clock time
        self.m.set_gun(self.k, GUN + timedelta(seconds=2.5))
        self.assertEqual(self.el("1"), 1796.5)

    def test_reads_dialog_race_time_stays(self):
        self.m.races[self.k].roster.add("1")
        self.m.ingest("1", GUN + timedelta(seconds=1700), "FINISH")
        rid = next(r.id for r in self.m.reads.values() if r.bib == "1")
        self.m.override_time(rid, GUN + timedelta(seconds=1720), elapsed=1720.0)
        self.m.set_gun(self.k, GUN + timedelta(seconds=3))
        self.assertEqual(self.el("1"), 1720.0)
        self.m.override_time(rid, None)                                                      # restored
        self.assertIsNone(self.m.reads[rid].typed_elapsed)

    def test_kept_across_a_restart(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            m = RaceManager(finish_readers={"FINISH"}, state_path=os.path.join(d, "s.json"))
            k = m.create_race("Boys", event=2, gender="M")
            m.set_gun(k, GUN)
            m.set_finish_time(k, "1", GUN + timedelta(seconds=1799), elapsed=1799.0)
            m._save()
            back = RaceManager(finish_readers={"FINISH"}, state_path=os.path.join(d, "s.json"))
            back.set_gun(k, GUN + timedelta(seconds=4))
            fin = next(r["finish"] for r in back.results(k)["finishers"] if r["bib"] == "1")
            self.assertAlmostEqual(fin["elapsed"], 1799.0, places=3)


class EmptyRace(unittest.TestCase):

    def test_no_flag_per_runner_in_a_race_with_no_entries(self):
        from rfid_finishlynx.meet import scoring
        from rfid_finishlynx.meet.model import Athlete, Entry, Meet, MeetInfo, Race
        meet = Meet(MeetInfo(id="m", name="Meet", date="2026-10-13"))
        meet.athletes.append(Athlete(id="a1", first="A", last="B", gender="M", bib="2000"))
        meet.athletes.append(Athlete(id="a2", first="C", last="D", gender="M", bib="2001"))
        race = Race(event=4)
        rm = {"finishers": [{"bib": "2000", "finish": {"elapsed": 1000.0}, "splits": []}], "on_course": []}
        res, warnings = scoring.build_results(meet, race, rm, final=False)
        self.assertEqual([w["code"] for w in warnings], [])
        self.assertEqual([f["bib"] for f in res["finishers"]], ["2000"])
        # a race WITH a start list still flags a runner who isn't on it
        race.entries.append(Entry(athlete_id="a2", event=4, auto=True))      # entered by finishing it
        res, warnings = scoring.build_results(meet, race, rm, final=False)
        self.assertEqual([w["code"] for w in warnings], [])
        race.entries[0].auto = False                                         # entered by the operator
        res, warnings = scoring.build_results(meet, race, rm, final=False)
        self.assertEqual([w["code"] for w in warnings], ["not_entered"])


if __name__ == "__main__":
    unittest.main()
