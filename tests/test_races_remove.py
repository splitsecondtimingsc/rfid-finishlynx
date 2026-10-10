import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.races import RaceManager


class TestRemoveBib(unittest.TestCase):

    def _mgr(self):
        mgr = RaceManager(athlete_lookup=lambda bib: {"gender": "F"}, on_change=lambda k: None,
                          chute_reader="CHUTE", finish_readers={"FINISH"}, point_labels={},
                          state_path=None)
        # a 2000 m race: finishes from ~7:30 are plausible (5 km would hold them as too early)
        key = mgr.create_race("JV Girls", event=4, gender="F", distance="2000")
        gun = datetime(2026, 9, 12, 9, 44, 0)
        mgr.set_gun(key, gun, "manual")
        return mgr, key, gun

    def test_multiple_reads_then_remove_clears_finisher(self):
        mgr, key, gun = self._mgr()
        for secs in (446, 458, 616, 628, 641):          # a tester walking back and forth
            mgr.ingest("16", gun + timedelta(seconds=secs), "FINISH")
        self.assertEqual([r["bib"] for r in mgr.results(key)["finishers"]], ["16"])
        # Deleting one read is not enough - she is still listed on the next one.
        last = mgr.results(key)["finishers"][0]["finish"]["read_id"]
        mgr.ignore_read(last)
        self.assertEqual([r["bib"] for r in mgr.results(key)["finishers"]], ["16"])
        # Remove from race takes all of them.
        n = mgr.remove_bib(key, "16")
        self.assertEqual(n, 4)
        self.assertEqual(mgr.results(key)["finishers"], [])
        self.assertNotIn("16", mgr.races[key].roster)
        self.assertEqual(len([r for r in mgr.ignored_reads() if r["bib"] == "16"]), 5)

    def test_remove_leaves_other_athletes_alone(self):
        mgr, key, gun = self._mgr()
        mgr.ingest("16", gun + timedelta(seconds=500), "FINISH")
        mgr.ingest("17", gun + timedelta(seconds=510), "FINISH")
        mgr.remove_bib(key, "16")
        self.assertEqual([r["bib"] for r in mgr.results(key)["finishers"]], ["17"])
        self.assertEqual(mgr.results(key)["finishers"][0]["place"], 1)


if __name__ == "__main__":
    unittest.main()
