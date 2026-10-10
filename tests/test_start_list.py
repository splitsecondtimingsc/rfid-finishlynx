"""Start list (2026-09-30, user's rule): chip times go to the .lif MeetPro
reads automatically - UNLESS the race has a start list (the .evt entries it
was made from, or a check-in). Then a finisher who is not on it is held
back, splits and all, flagged "Save to .lif" at once, and goes in only when
the timer releases them. FinishLynx's own rows are never affected.
python -m unittest tests.test_start_list"""

import csv
import unittest
from datetime import timedelta

from rfid_finishlynx import web
from tests.test_split_lif_contract import GUN, race_with_splits


def lif_rows(mgr, key, fl_text=None):
    text = mgr.lif_content(key, fl_text=fl_text)
    return {r[1]: r for r in list(csv.reader([l for l in text.splitlines() if l.strip()]))[1:] if r[1]}


def finish_of(mgr, key, bib):
    return next(r for r in mgr.results(key)["finishers"] if r["bib"] == bib)["finish"]


class TestNoStartList(unittest.TestCase):

    def test_chip_times_flow_as_before(self):
        mgr, key = race_with_splits()
        self.assertEqual(set(lif_rows(mgr, key)), {"1", "2", "3"})
        self.assertEqual(finish_of(mgr, key, "3")["lif_why"], "")


class TestEntriesFromTheEvt(unittest.TestCase):

    def setUp(self):
        self.mgr, self.key = race_with_splits()
        self.mgr.set_entries(self.key, ["1", "2", "7"])        # 3 is not entered

    def test_off_list_finisher_is_held_and_flagged_at_once(self):
        self.assertNotEqual(self.mgr.races[self.key].status, "final")   # still running: flagged anyway
        self.assertNotIn("3", lif_rows(self.mgr, self.key))
        fin = finish_of(self.mgr, self.key, "3")
        self.assertEqual((fin["fl_untimed"], fin["lif_why"]), (True, "off_list"))
        self.assertEqual(finish_of(self.mgr, self.key, "1")["lif_why"], "")

    def test_released_goes_in_with_its_splits(self):
        self.mgr.set_chip_to_lif(self.key, "3")
        row = lif_rows(self.mgr, self.key)["3"]
        self.assertEqual(row[6], "18:45.000")
        self.assertTrue(row[10].startswith("6:05.000"), row[10])         # Mile 1 came along

    def test_on_course_off_list_runner_shows_nothing_yet(self):
        self.mgr.ingest("9", GUN + timedelta(seconds=400), "S1")        # not entered, Mile 1 only
        self.mgr.races[self.key].roster.discard("9")
        self.assertNotIn("9", lif_rows(self.mgr, self.key))
        self.mgr.ingest("7", GUN + timedelta(seconds=390), "S1")        # entered: shows on course
        self.assertIn("7", lif_rows(self.mgr, self.key))

    def test_entrants_are_listed_until_read(self):
        res = self.mgr.results(self.key)
        self.assertEqual([r["bib"] for r in res["not_seen"]], ["7"])

    def test_finishlynx_timed_rows_win_regardless(self):
        fl = ("2,1,1,Girls 5K,,,,,,5000,09:00:00.000\r\n"
              "1,3,,Cole,Cy,Red,18:44.900,,18:44.900,,,09:00:00.000,F,,,,,,18:44.900\r\n")
        self.mgr._fl_decided[self.key] = {"3"}
        self.assertEqual(lif_rows(self.mgr, self.key, fl)["3"][6], "18:44.900")
        self.assertEqual(finish_of(self.mgr, self.key, "3")["lif_why"], "")


class TestCheckInStartList(unittest.TestCase):

    def test_missed_check_in_waits_for_save(self):
        mgr, key = race_with_splits()
        mgr.races[key].checkins.update({"1": "x", "2": "x"})           # 3 missed the chute
        self.assertNotIn("3", lif_rows(mgr, key))
        self.assertEqual(finish_of(mgr, key, "3")["lif_why"], "off_list")


def _held(mgr, bib, point):
    """Put the bib's read at `point` into Held, as if the panes had kept it out."""
    read = next(r for r in mgr.reads.values() if r.bib == bib and r.point == point)
    read.state, read.race, read.reason = "held", None, "no open race"
    return read.id


class TestAssignedFromHeld(unittest.TestCase):
    """User's rule 2026-10-03: a finish the operator moves from Held into a
    race goes into the .lif at once - no separate "Save to .lif"."""

    def setUp(self):
        self.mgr, self.key = race_with_splits()
        self.mgr.set_entries(self.key, ["1", "2"])              # 3 is off the list

    def test_hand_placed_finish_goes_into_the_lif(self):
        self.assertNotIn("3", lif_rows(self.mgr, self.key))
        self.mgr.assign_read(_held(self.mgr, "3", "FINISH"), self.key)
        self.assertIn("3", self.mgr.races[self.key].chip_to_lif)
        self.assertEqual(lif_rows(self.mgr, self.key)["3"][6], "18:45.000")
        self.assertEqual(finish_of(self.mgr, self.key, "3")["lif_why"], "")

    def test_hand_placed_split_does_not(self):
        self.mgr.assign_read(_held(self.mgr, "3", "S1"), self.key)
        self.assertNotIn("3", self.mgr.races[self.key].chip_to_lif)
        self.assertNotIn("3", lif_rows(self.mgr, self.key))

    def test_mat_placed_finish_still_waits(self):
        self.mgr.ingest("9", GUN + timedelta(seconds=1200), "FINISH")
        self.assertNotIn("9", self.mgr.races[self.key].chip_to_lif)
        self.assertNotIn("9", lif_rows(self.mgr, self.key))

    def test_finishlynx_timed_row_still_wins(self):
        self.mgr.assign_read(_held(self.mgr, "3", "FINISH"), self.key)
        fl = ("2,1,1,Girls 5K,,,,,,5000,09:00:00.000\r\n"
              "1,3,,Cole,Cy,Red,18:44.900,,18:44.900,,,09:00:00.000,F,,,,,,18:44.900\r\n")
        self.mgr._fl_decided[self.key] = {"3"}
        self.assertEqual(lif_rows(self.mgr, self.key, fl)["3"][6], "18:44.900")


class TestEvtParsing(unittest.TestCase):

    def test_evt_race_list_carries_entry_bibs(self):
        evt = ("1,1,1,Girls 5000 Meters,,,,,,5000\r\n,101,1,Ames,Ava,Red\r\n,102,2,Bell,Bo,Blue\r\n"
               "2,1,1,4x400 Relay\r\n,,1,Red,,Red A\r\n")
        races = web.evt_race_list(evt)
        self.assertEqual(races[0]["bibs"], ["101", "102"])
        self.assertEqual(races[1]["bibs"], [])                          # relays: no bibs

    def test_create_from_evt_sets_the_start_list(self):
        from types import SimpleNamespace
        from rfid_finishlynx.races import RaceManager
        mgr = RaceManager(state_path=None)
        web._application = SimpleNamespace(race_manager=mgr, offer_recent_starts=lambda k: None)
        self.addCleanup(setattr, web, "_application", None)
        r = web.app.test_client().post("/api/races/create", json={
            "name": "Girls 5000", "event": 1, "entries": ["101", "102"]})
        key = r.get_json()["key"]
        self.assertEqual(mgr.races[key].entries, {"101", "102"})
        self.assertTrue({"101", "102"} <= mgr.races[key].roster)


if __name__ == "__main__":
    unittest.main()
