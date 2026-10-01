"""Athletes FinishLynx has timed but the reader never read at the finish
show in the pane with FinishLynx's time, flagged "FL only" (2026-10-01,
user's ask). python -m unittest tests.test_fl_only"""

import csv
import io
import os
import shutil
import tempfile
import unittest
from datetime import timedelta

from tests.test_lif_merge import FL_TEXT, rows_of
from tests.test_split_lif_contract import GUN, race_with_splits


class TestFinishLynxOnly(unittest.TestCase):

    def setUp(self):
        self.lynx = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.lynx, True)
        self.rfid = os.path.join(self.lynx, "RFID")
        self.mgr, self.key = race_with_splits()
        path = os.path.join(self.lynx, "002-1-01.lif")
        with open(path, "w", newline="") as f:
            f.write(FL_TEXT)
        t = (GUN + timedelta(minutes=19)).timestamp()
        os.utime(path, (t, t))

    def merge(self):
        self.mgr.write_merged_lif(self.key, self.lynx, self.rfid)
        return self.mgr.results(self.key)

    def test_timed_only_in_finishlynx_is_listed_and_flagged(self):
        res = self.merge()
        row = next(r for r in res["finishers"] if r["bib"] == "99")
        self.assertTrue(row["finish"]["fl_only"])
        self.assertAlmostEqual(row["finish"]["elapsed"], 19 * 60.0)
        self.assertIsNone(row["finish"]["read_id"])
        self.assertEqual((row["last_name"], row["first_name"], row["school"]), ("Zed", "Zoe", "Blue"))
        self.assertEqual(row["place"], 4)                    # after the three chip finishers
        reader_rows = [r for r in res["finishers"] if r["bib"] != "99"]
        self.assertFalse(any(r["finish"].get("fl_only") for r in reader_rows))

    def test_dnf_and_bibless_rows_are_not_added(self):
        bibs = {r["bib"] for r in self.merge()["finishers"]}
        self.assertNotIn("98", bibs)                         # flagged DNF
        self.assertNotIn("", bibs)                           # unidentified finisher

    def test_merged_lif_has_them_once(self):
        self.merge()
        with open(os.path.join(self.rfid, "002-1-01.lif"), newline="") as f:
            _, rows = rows_of(f.read())
        self.assertEqual([r[1] for r in rows].count("99"), 1)

    def test_splits_read_but_finish_missed_gets_finishlynx_finish(self):
        self.mgr.races[self.key].roster.add("99")
        self.mgr.ingest("99", GUN + timedelta(seconds=370), "S1")
        row = next(r for r in self.merge()["finishers"] if r["bib"] == "99")
        self.assertTrue(row["finish"]["fl_only"])
        self.assertEqual([s["point"] for s in row["splits"]], ["S1"])

    def test_a_reader_finish_wins(self):
        self.mgr.races[self.key].roster.add("99")
        self.mgr.ingest("99", GUN + timedelta(seconds=1139.5), "FINISH")
        row = next(r for r in self.merge()["finishers"] if r["bib"] == "99")
        self.assertFalse(row["finish"].get("fl_only"))
        self.assertIsNotNone(row["finish"]["read_id"])

    def test_entrant_is_not_also_listed_as_not_seen(self):
        self.mgr.set_entries(self.key, ["1", "2", "3", "99"])
        res = self.merge()
        self.assertNotIn("99", [r["bib"] for r in res["not_seen"]])

    def test_csv_says_finishlynx_only(self):
        self.merge()
        rows = list(csv.reader(io.StringIO(self.mgr.results_csv([self.key]))))
        row = next(r for r in rows if len(r) > 6 and r[6] == "99")
        self.assertEqual(row[4], "finished (FinishLynx only)")

    def test_gone_from_finishlynx_file_gone_from_pane(self):
        self.merge()
        path = os.path.join(self.lynx, "002-1-01.lif")
        with open(path, "w", newline="") as f:
            f.write("\r\n".join(l for l in FL_TEXT.split("\r\n") if ",99," not in l))
        t = (GUN + timedelta(minutes=20)).timestamp()
        os.utime(path, (t, t))
        self.assertNotIn("99", [r["bib"] for r in self.merge()["finishers"]])


if __name__ == "__main__":
    unittest.main()
