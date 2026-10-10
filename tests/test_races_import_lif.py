"""2026-10-09 (Jim Crawford): "Select .lif file" - a .lif the operator picks
is the truth for the race. Bib 1439 crossed the finish mat at 26:33 and
again at 39:18 walking back; the last finish read won and FinishLynx's
corrected file could not override it. Hardware-free:
python -m unittest tests.test_races_import_lif
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from rfid_finishlynx.races import RaceManager

GUN = datetime(2026, 10, 8, 17, 4, 26)

ATHLETES = {b: {"last_name": f"L{b}", "first_name": f"F{b}", "school": "North", "gender": "F"}
            for b in ("101", "102", "103", "104", "105", "106")}


def mgr(state_path=None):
    return RaceManager(athlete_lookup=ATHLETES.get, chute_reader="CHUTE", finish_readers={"FIN"},
                       point_labels={"S1": "Mile 1"}, state_path=state_path)


# FinishLynx numbers the event 2 although the console's race is event 1:
# the import doesn't care (the operator picked the race).
LIF = ("2,1,1,Girls 5K,,,,,,5000,17:04:26.3767\r\n"
       "1,103,,L103,F103,North,19:58.120,,,,,17:04:26.377,F,,,,,,\r\n"
       "2,101,,L101,F101,North,20:00.500,,,,,17:04:26.377,F,,,,,,\r\n"
       "3,104,,L104,F104,North,23:10.000,,,,,17:04:26.377,F,,,,,,\r\n"
       "DNF,105,,L105,F105,North,,,,,,,F,,,,,,\r\n"
       ",106,,L106,F106,North,,,,,,,F,,,,,,\r\n")


class TestImportLif(unittest.TestCase):

    def setUp(self):
        self.m = mgr()
        self.key = self.m.create_race("Girls 5k", event=1, gender="F")
        self.m.set_gun(self.key, GUN)
        at = lambda s: GUN + timedelta(seconds=s)          # noqa: E731
        self.m.ingest("101", at(1200), "FIN")              # her real finish, 20:00
        self.m.ingest("101", at(2358), "FIN")              # walking back, 39:18
        self.m.ingest("102", at(1260), "FIN")              # not in the file: reads count
        self.m.ingest("103", at(1800), "FIN")              # mat 30:00, film 19:58.12
        self.m.ingest("105", at(1500), "FIN")              # read, but DNF in the file

    def order(self):
        return [(r["bib"], round(r["finish"]["elapsed"], 2))
                for r in self.m.results(self.key)["finishers"]]

    def test_the_walk_back_is_not_a_second_finish(self):
        # one read per station per race (2026-10-09); before, the last
        # finish read won and 101 was last at 39:18
        self.assertEqual(self.order()[0], ("101", 1200.0))
        self.assertEqual(self.m.results(self.key)["extra_crossings"][0]["bib"], "101")

    def test_picked_lif_is_the_truth(self):
        s = self.m.import_lif(self.key, LIF, r"C:\Finish_Lynx_Results\001-1-01.lif")
        self.assertEqual(s["name"], "001-1-01.lif")
        self.assertEqual((s["timed"], s["flagged"]), (3, 1))
        self.assertEqual(s["header"]["event"], "2")
        # 103 and 101 from the file, 102 from the reads, 104 only in the
        # file, 105 DNF there: no finish whatever the mat read
        self.assertEqual(self.order(), [("103", 1198.12), ("101", 1200.5), ("102", 1260.0),
                                        ("104", 1390.0)])
        fin = {r["bib"]: r["finish"] for r in self.m.results(self.key)["finishers"]}
        self.assertTrue(fin["101"]["from_lif"])
        self.assertFalse(fin["101"]["fl_only"])
        self.assertTrue(fin["104"]["fl_only"])
        self.assertNotIn("from_lif", fin["102"])
        moved = {m["bib"]: (m["old_place"], m["place"]) for m in s["moved"]}
        self.assertEqual(moved["101"], (1, 2))
        self.assertEqual(moved["104"], (None, 4))
        self.assertEqual([g["bib"] for g in s["gone"]], ["105"])
        # the file stands in for FinishLynx's own one (merged .lif, Save .lif)
        self.assertEqual(self.m.finishlynx_lif(self.key, ""), LIF)

    def test_reads_still_add_finishers_while_open(self):
        self.m.import_lif(self.key, LIF, "a.lif")
        self.m.ingest("106", GUN + timedelta(seconds=1500), "FIN")    # untimed in the file
        self.assertIn(("106", 1500.0), self.order())
        self.m.ingest("101", GUN + timedelta(seconds=2500), "FIN")    # another walk back
        self.assertIn(("101", 1200.5), self.order())

    def test_clear_goes_back_to_the_reads(self):
        self.m.import_lif(self.key, LIF, "a.lif")
        self.assertTrue(self.m.clear_lif(self.key))
        self.assertEqual(self.order()[0], ("101", 1200.0))
        self.assertFalse(self.m.clear_lif(self.key))

    def test_not_a_lif_is_refused(self):
        with self.assertRaises(ValueError):
            self.m.import_lif(self.key, "hello\r\n", "x.lif")
        with self.assertRaises(ValueError):
            self.m.import_lif(self.key, "2,1,1,Girls 5K\r\n,106,,L,F,North\r\n", "x.lif")
        self.assertEqual(self.m.races[self.key].lif_truth, "")

    def test_kept_across_a_restart(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "races-state.json")
        m = mgr(path)
        k = m.create_race("Girls 5k", event=1, gender="F")
        m.set_gun(k, GUN)
        m.import_lif(k, LIF, "001-1-01.lif")
        m2 = mgr(path)
        self.assertEqual(m2.races[k].lif_truth, LIF)
        self.assertEqual(m2.races[k].lif_truth_name, "001-1-01.lif")
        self.assertEqual([r["bib"] for r in m2.results(k)["finishers"]], ["103", "101", "104"])

    def test_a_time_set_by_hand_wins_over_the_picked_lif(self):
        # the Results page's Edit time (2026-10-09, user: your edit wins)
        self.m.import_lif(self.key, LIF, "a.lif")
        self.m.set_finish_time(self.key, "101", GUN + timedelta(seconds=1250))   # reader finish, retimed
        self.m.set_finish_time(self.key, "104", GUN + timedelta(seconds=1180))   # only in the file: typed
        order = self.order()
        self.assertEqual(order[0], ("104", 1180.0))
        self.assertIn(("101", 1250.0), order)
        self.assertNotIn("from_lif", next(r for r in self.m.results(self.key)["finishers"] if r["bib"] == "104")["finish"])

    def test_remove_takes_a_runner_out_of_the_picked_lif_too(self):
        self.m.import_lif(self.key, LIF, "a.lif")
        self.m.remove_bib(self.key, "104")
        self.assertNotIn("104", [b for b, _ in self.order()])

    def test_finish_time_api(self):
        from types import SimpleNamespace
        from rfid_finishlynx import web
        web._application = SimpleNamespace(race_manager=self.m)
        c = web.app.test_client()
        r = c.post(f"/api/races/{self.key}/finish_time", json={"bib": "102", "time": "20:59.5"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn(("102", 1259.5), self.order())
        # a clock time is on the race's own day
        r = c.post(f"/api/races/{self.key}/finish_time", json={"bib": "102", "time": "17:25:26.000"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn(("102", 1260.0), self.order())
        r = c.post(f"/api/races/{self.key}/finish_time", json={"bib": "102", "time": "17:00:00"})
        self.assertEqual(r.status_code, 400)                           # before the gun

    def test_m_reverts_a_time_set_by_hand(self):
        # the race pane's "M" (2026-10-10, user): back to what was read
        self.m.import_lif(self.key, LIF, "a.lif")
        self.m.set_finish_time(self.key, "102", GUN + timedelta(seconds=1250))   # reader finish 1260
        self.assertIn(("102", 1250.0), self.order())
        self.assertEqual(self.m.revert_finish(self.key, "102")["how"], "back to the read time")
        self.assertIn(("102", 1260.0), self.order())
        # a reader finish the .lif times: back to the file's time
        self.m.set_finish_time(self.key, "101", GUN + timedelta(seconds=1111))
        self.m.revert_finish(self.key, "101")
        self.assertIn(("101", 1200.5), self.order())
        # typed with no read behind it: removed, the file's time comes back
        self.m.set_finish_time(self.key, "104", GUN + timedelta(seconds=1180))
        self.assertEqual(self.m.revert_finish(self.key, "104")["how"], "typed time removed")
        self.assertIn(("104", 1390.0), self.order())
        with self.assertRaises(ValueError):
            self.m.revert_finish(self.key, "103")                          # nothing set by hand

    def test_revert_api(self):
        from types import SimpleNamespace
        from rfid_finishlynx import web
        web._application = SimpleNamespace(race_manager=self.m)
        c = web.app.test_client()
        c.post(f"/api/races/{self.key}/finish_time", json={"bib": "102", "time": "20:50"})
        r = c.post(f"/api/races/{self.key}/revert_finish", json={"bib": "102"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn(("102", 1260.0), self.order())
        self.assertEqual(c.post(f"/api/races/{self.key}/revert_finish", json={"bib": "102"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
