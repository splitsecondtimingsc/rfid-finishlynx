"""The meet's .evt is watched once a meet is loaded (2026-10-01, user's ask):
when MeetPro rewrites it, the panes made from it follow - late entries join
the start list, scratches leave it (never one who already ran), and a
renamed event / changed distance carries over unless the pane was set by
hand. New events only light up "Races from .evt"; no pane is made.
python -m unittest tests.test_evt_autoupdate"""

import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.races import RaceManager

GUN = datetime(2026, 10, 1, 9, 0, 0)

EVT_V1 = ("1,1,1,Girls 5000 Meters,,,,,,5000\r\n"
          ",101,1,Ames,Ava,Red\r\n,102,2,Bell,Bo,Blue\r\n,103,3,Cole,Cy,Red\r\n"
          "2,1,1,Boys 5000 Meters,,,,,,5000\r\n,201,1,Dunn,Di,Red\r\n"
          "3,1,1,4x400 Relay\r\n,,1,Red,,Red A\r\n")


def evt(text):
    return web.evt_race_list(text)


class TestApplyEvt(unittest.TestCase):

    def setUp(self):
        self.m = RaceManager(state_path=None)
        self.girls = self.m.create_race("Girls 5000 Meters", event=1, gender="F", distance="5000")
        self.m.set_entries(self.girls, ["101", "102", "103"])
        self.prev = evt(EVT_V1)

    def apply(self, text, previous="v1"):
        return self.m.apply_evt(evt(text), self.prev if previous == "v1" else previous)

    def test_late_entry_joins_the_start_list(self):
        out = self.apply(EVT_V1.replace(",103,3,Cole,Cy,Red\r\n", ",103,3,Cole,Cy,Red\r\n,104,4,Diaz,Dee,Blue\r\n"))
        race = self.m.races[self.girls]
        self.assertIn("104", race.entries)
        self.assertIn("104", race.roster)
        self.assertEqual((out["entries_added"], out["entries_removed"]), (1, 0))

    def test_scratch_leaves_the_start_list(self):
        out = self.apply(EVT_V1.replace(",102,2,Bell,Bo,Blue\r\n", ""))
        race = self.m.races[self.girls]
        self.assertNotIn("102", race.entries)
        self.assertNotIn("102", race.roster)
        self.assertEqual(out["entries_removed"], 1)

    def test_scratch_after_they_ran_keeps_them_entered(self):
        self.m.set_gun(self.girls, GUN)
        self.m.ingest("102", GUN + timedelta(minutes=18), "FINISH")
        self.apply(EVT_V1.replace(",102,2,Bell,Bo,Blue\r\n", ""))
        race = self.m.races[self.girls]
        self.assertIn("102", race.entries)          # not held back as off-list
        self.assertIn("102", race.roster)
        fin = next(r for r in self.m.results(self.girls)["finishers"] if r["bib"] == "102")
        self.assertEqual(fin["finish"]["lif_why"], "")

    def test_checked_in_scratch_stays_on_the_roster(self):
        self.m.races[self.girls].checkins["103"] = "x"
        self.apply(EVT_V1.replace(",103,3,Cole,Cy,Red\r\n", ""))
        race = self.m.races[self.girls]
        self.assertNotIn("103", race.entries)
        self.assertIn("103", race.roster)

    def test_held_read_of_a_late_entry_is_placed(self):
        self.m.races[self.girls].gender = ""        # roster only
        self.m.set_gun(self.girls, GUN)
        self.m.ingest("104", GUN + timedelta(minutes=18), "FINISH")
        self.assertEqual([r["bib"] for r in self.m.results(self.girls)["finishers"]], [])
        self.apply(EVT_V1.replace(",103,3,Cole,Cy,Red\r\n", ",103,3,Cole,Cy,Red\r\n,104,4,Diaz,Dee,Blue\r\n"))
        self.assertEqual([r["bib"] for r in self.m.results(self.girls)["finishers"]], ["104"])

    def test_renamed_event_and_new_distance_carry_over(self):
        out = self.apply(EVT_V1.replace("Girls 5000 Meters,,,,,,5000", "Girls 4000 Meters,,,,,,4000"))
        race = self.m.races[self.girls]
        self.assertEqual((race.name, race.distance), ("Girls 4000 Meters", "4000"))
        self.assertEqual(out["renamed"], ["Girls 5000 Meters -> Girls 4000 Meters"])

    def test_pane_named_by_hand_keeps_its_name(self):
        self.m.races[self.girls].name = "Varsity Girls"
        self.apply(EVT_V1.replace("Girls 5000 Meters", "Girls 5K"))
        self.assertEqual(self.m.races[self.girls].name, "Varsity Girls")

    def test_no_rename_without_the_previous_file(self):
        out = self.apply(EVT_V1.replace("Girls 5000 Meters", "Girls 5K"), previous=None)
        self.assertEqual(self.m.races[self.girls].name, "Girls 5000 Meters")
        self.assertEqual(out["new_races"], [])

    def test_new_event_is_reported_not_created(self):
        out = self.apply(EVT_V1 + "4,1,1,JV Girls 3000 Meters,,,,,,3000\r\n,301,1,Eve,Em,Red\r\n")
        self.assertEqual(out["new_races"], ["JV Girls 3000 Meters"])
        self.assertEqual(len(self.m.races), 1)

    def test_unchanged_file_changes_nothing(self):
        out = self.apply(EVT_V1)
        self.assertFalse(any(out.values()))

    def test_event_gone_from_the_file_keeps_its_pane(self):
        self.apply("2,1,1,Boys 5000 Meters,,,,,,5000\r\n,201,1,Dunn,Di,Red\r\n")
        self.assertEqual(self.m.races[self.girls].entries, {"101", "102", "103"})


class TestWatcher(unittest.TestCase):
    """Load meet -> the .evt is watched; a MeetPro rewrite reaches the pane."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "lynx.evt")
        self.write(EVT_V1, age=10)
        self.mgr = RaceManager(state_path=None)
        cfg = SimpleNamespace(event_directory=self.dir, evt_file="lynx.evt",
                              races=SimpleNamespace(rfid_subdir="RFID"))
        web._application = SimpleNamespace(config=cfg, race_manager=self.mgr, save_config=lambda: None)
        self.addCleanup(setattr, web, "_application", None)
        saved = dict(web._evt_monitor_state)
        self.addCleanup(lambda: (web.stop_evt_monitor(), web._evt_monitor_state.update(saved)))
        self.girls = self.mgr.create_race("Girls 5000 Meters", event=1, gender="F", distance="5000")
        self.mgr.set_entries(self.girls, ["101", "102", "103"])
        web._autoload_evt(self.dir)

    def write(self, text, age=0.0):
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        t = time.time() - age
        os.utime(self.path, (t, t))

    def test_meet_load_starts_the_watch(self):
        st = web._evt_monitor_state
        self.assertTrue(st["monitor_running"])
        self.assertEqual((st["monitored_dir"], st["monitored_file"]), (self.dir, "lynx.evt"))
        self.assertIsNone(st["config"])                 # read-only: never rewrites MeetPro's file

    def test_rewrite_reaches_the_pane(self):
        self.write(EVT_V1.replace(",102,2,Bell,Bo,Blue\r\n", ",104,2,Diaz,Dee,Blue\r\n"), age=5)
        web._attempt_evt_autosave()
        self.assertEqual(self.mgr.races[self.girls].entries, {"101", "103", "104"})
        sync = web._races_payload()["evt_sync"]
        self.assertEqual((sync["entries_added"], sync["entries_removed"], sync["file"]), (1, 1, "lynx.evt"))
        with open(self.path, encoding="utf-8") as f:
            self.assertIn(",104,2,Diaz", f.read())      # file untouched

    def test_file_still_being_written_waits(self):
        self.write(EVT_V1.replace(",102,2,Bell,Bo,Blue\r\n", ""), age=0)
        web._attempt_evt_autosave()
        self.assertIn("102", self.mgr.races[self.girls].entries)
        os.utime(self.path, (time.time() - 5, time.time() - 5))
        web._attempt_evt_autosave()
        self.assertNotIn("102", self.mgr.races[self.girls].entries)

    def test_start_list_in_the_rfid_subfolder(self):
        sub = os.path.join(self.dir, "RFID")
        os.makedirs(sub)
        shutil.move(self.path, os.path.join(sub, "lynx.evt"))
        self.path = os.path.join(sub, "lynx.evt")
        web._autoload_evt(self.dir)
        self.assertEqual(web._evt_monitor_state["monitored_dir"], sub)
        self.write(EVT_V1.replace(",101,1,Ames,Ava,Red\r\n", ""), age=5)
        web._attempt_evt_autosave()
        self.assertNotIn("101", self.mgr.races[self.girls].entries)


if __name__ == "__main__":
    unittest.main()
