"""The merged .lif in the RFID folder: FinishLynx's rows + RFID splits.

Layout (lif_merge.py): FinishLynx saves into the Lynx results folder;
MeetPro's F.A.T. directory is its RFID subfolder, where the console writes
the merged EEE-R-HH.lif. FinishLynx's rows win for place/time/names, the
console supplies splits (split points only, stopping at a gap), chip times
are published for finishers FinishLynx has not placed yet, and everything
is measured from FinishLynx's start when it has one.
Hardware-free: python -m unittest tests.test_lif_merge
"""
import csv
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from rfid_finishlynx import web
from rfid_finishlynx.lif_merge import meet_dirs, start_list_dir, wipe_merged_lifs
from tests import meetpro_model as mp
from tests.test_split_lif_contract import GUN, race_with_splits

# FinishLynx's own 002-1-01.lif mid-race: its clock started 1 s after the
# console's gun, it has evaluated bibs 1 and 2 (film times), an
# unidentified finisher, a runner without a chip (99) and a DNF (98).
FL_TEXT = "\r\n".join([
    "2,1,1,Girls 5K,,,,,,5000,09:00:01.000",
    "1,1,,Ames,Ava,Red,18:19.400,,18:19.400,,18:19.4 (18:19.4),09:00:01.000,F,,,18:19.400,18:19.400,4.55,18:19.400",
    "2,2,,Bell,Bo,Blue,18:29.100,,9.700,,18:29.1 (18:29.1),09:00:01.000,F,,,9.700,9.700,4.51,18:29.100",
    "3,,,,,,18:40.000,,10.900,,18:40.0 (18:40.0),09:00:01.000,,,,10.900,10.900,,18:40.000",
    "4,99,,Zed,Zoe,Blue,19:00.000,,20.000,,19:00.0 (19:00.0),09:00:01.000,F,,,20.000,20.000,4.38,19:00.000",
    "DNF,98,,Dee,Nef,Red,,,,,,09:00:01.000,F,,,,,,",
]) + "\r\n"


def rows_of(text):
    lines = [l for l in text.splitlines() if l.strip()]
    rows = list(csv.reader(lines))
    return rows[0], rows[1:]


class TestFolders(unittest.TestCase):

    def test_meet_folder_is_the_lynx_results_folder(self):
        self.assertEqual(meet_dirs(r"C:\Finish_Lynx_Results"),
                         (r"C:\Finish_Lynx_Results", r"C:\Finish_Lynx_Results\RFID"))

    def test_meet_folder_may_be_the_rfid_folder_itself(self):
        self.assertEqual(meet_dirs(r"C:\Finish_Lynx_Results\RFID"),
                         (r"C:\Finish_Lynx_Results", r"C:\Finish_Lynx_Results\RFID"))
        self.assertEqual(meet_dirs(r"C:\Finish_Lynx_Results\rfid\\"),
                         (r"C:\Finish_Lynx_Results", r"C:\Finish_Lynx_Results\rfid"))

    def test_explicit_lif_dir_wins(self):
        self.assertEqual(meet_dirs(r"C:\meet", r"D:\Lynx", "MP"), (r"D:\Lynx", r"D:\Lynx\MP"))

    def test_start_lists_come_from_rfid_once_meetpro_writes_there(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.assertEqual(start_list_dir(d), d)                 # old layout: the folder itself
        os.makedirs(os.path.join(d, "RFID"))
        open(os.path.join(d, "RFID", "lynx.evt"), "w").close()
        self.assertEqual(start_list_dir(d), os.path.join(d, "RFID"))


class TestMergeWithFinishLynx(unittest.TestCase):

    def setUp(self):
        self.mgr, self.key = race_with_splits()
        self.header, rows = rows_of(self.mgr.lif_content(self.key, fl_text=FL_TEXT))
        self.rows = rows
        self.by_bib = {r[1]: r for r in rows if r[1]}

    def test_finishlynx_header_and_start(self):
        self.assertEqual(self.header[10], "09:00:01.000")

    def test_finishlynx_time_and_names_win(self):
        r = self.by_bib["1"]
        self.assertEqual((r[3], r[6]), ("Ames", "18:19.400"))

    def test_splits_are_measured_from_finishlynx_start(self):
        # Mile 1 read 6:00.000 after the console gun = 5:59.000 after FinishLynx's
        self.assertEqual(self.by_bib["1"][10], "5:59.000 (5:59.000),12:09.250 (6:10.250)")

    def test_missing_split_still_held(self):
        self.assertEqual(self.by_bib["2"][10], "")

    def test_chip_time_published_until_finishlynx_places_them(self):
        r = self.by_bib["3"]                     # not in FinishLynx's file yet
        self.assertEqual(r[6], "18:44.000")      # 1125 s after the gun, 1124 s after FinishLynx's start
        self.assertEqual(r[10], "6:04.000 (6:04.000),12:24.000 (6:20.000)")

    def test_places_follow_time_across_both_sources(self):
        order = [(r[0], r[1]) for r in self.rows]
        self.assertEqual(order, [("1", "1"), ("2", "2"), ("3", ""), ("4", "3"), ("5", "99"), ("DNF", "98")])

    def test_rows_without_chip_data_carry_no_splits(self):
        self.assertEqual(self.by_bib["99"][10], "")
        self.assertEqual([r for r in self.rows if not r[1]][0][10], "")    # unidentified finisher

    def test_meetpro_reads_it_right(self):
        text = self.mgr.lif_content(self.key, fl_text=FL_TEXT)
        sent = mp.ms_live_sync("002-1-01.lif", text, ["Mile 1", "Mile 2"], {"Red", "Blue"})
        self.assertEqual(sent[1]["splits"], {"Mile 1": "5:59.000", "Mile 2": "12:09.250"})
        self.assertEqual(sent[2]["splits"], {})
        self.assertEqual(sent[1]["result"], "18:19.400")


class TestUntimedFinishLynxRow(unittest.TestCase):
    """2026-09-30: a Men's 5000 straggler finished after the event was done
    in FinishLynx; its file lists him entered but untimed. User's rule: the
    chip time never goes in by itself (a DNF walking past the mat must not
    become a result) - the pane flags him and the timer releases it."""

    UNTIMED = ",3,11,Cole,Cy,Red,,,,,,09:00:01.000,F,,,,,,"

    def merged(self, row3, release=False):
        mgr, key = race_with_splits()
        if release:
            mgr.set_chip_to_lif(key, "3")
        _, rows = rows_of(mgr.lif_content(key, fl_text=FL_TEXT + row3 + "\r\n"))
        return rows

    def test_untimed_row_stays_empty_until_released(self):
        mine = [r for r in self.merged(self.UNTIMED) if r[1] == "3"]
        self.assertEqual([(r[0], r[6]) for r in mine], [("", "")])

    def test_released_gets_the_chip_finish_with_finishlynx_names(self):
        rows = self.merged(self.UNTIMED, release=True)
        mine = [r for r in rows if r[1] == "3"]
        self.assertEqual(len(mine), 1, rows)                 # the empty row is replaced, not doubled
        self.assertEqual(mine[0][6], "18:44.000")
        self.assertEqual((mine[0][2], mine[0][3], mine[0][4], mine[0][5]), ("11", "Cole", "Cy", "Red"))
        self.assertEqual([(r[0], r[1]) for r in rows][:5],
                         [("1", "1"), ("2", "2"), ("3", ""), ("4", "3"), ("5", "99")])

    def test_a_flag_set_in_finishlynx_wins_even_when_released(self):
        mine = [r for r in self.merged("DQ,3,11,Cole,Cy,Red,,,,,,09:00:01.000,F,,,,,,", release=True)
                if r[1] == "3"]
        self.assertEqual([(r[0], r[6]) for r in mine], [("DQ", "")])

    def test_pane_flags_the_untimed_finisher_until_released(self):
        from rfid_finishlynx.lif_merge import untimed_bibs
        self.assertEqual(untimed_bibs(FL_TEXT + self.UNTIMED + "\r\n"), {"3"})   # DNF 98 is flagged, not untimed
        mgr, key = race_with_splits()
        mgr._fl_untimed[key] = {"3"}
        fin = next(r for r in mgr.results(key)["finishers"] if r["bib"] == "3")["finish"]
        self.assertFalse(fin["fl_untimed"])                  # still racing: FinishLynx not done yet
        mgr.set_status(key, "final")
        fin = next(r for r in mgr.results(key)["finishers"] if r["bib"] == "3")["finish"]
        self.assertEqual((fin["fl_untimed"], fin["chip_in_lif"]), (True, False))
        mgr.set_chip_to_lif(key, "3")
        fin = next(r for r in mgr.results(key)["finishers"] if r["bib"] == "3")["finish"]
        self.assertEqual((fin["fl_untimed"], fin["chip_in_lif"]), (False, True))
        mgr.set_chip_to_lif(key, "3", on=False)                                    # undo
        fin = next(r for r in mgr.results(key)["finishers"] if r["bib"] == "3")["finish"]
        self.assertEqual((fin["fl_untimed"], fin["chip_in_lif"]), (True, False))


class TestWriteMerged(unittest.TestCase):

    def setUp(self):
        self.lynx = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.lynx, True)
        self.rfid = os.path.join(self.lynx, "RFID")
        self.mgr, self.key = race_with_splits()
        self.fl_path = os.path.join(self.lynx, "002-1-01.lif")

    def read(self):
        with open(os.path.join(self.rfid, "002-1-01.lif"), newline="") as f:
            return f.read()

    def write_fl(self, text, saved=GUN + timedelta(minutes=19)):
        """FinishLynx saves its file; dated during the race by default."""
        with open(self.fl_path, "w", newline="") as f:
            f.write(text)
        os.utime(self.fl_path, (saved.timestamp(), saved.timestamp()))

    def test_writes_into_rfid_and_never_into_finishlynx_folder(self):
        path = self.mgr.write_merged_lif(self.key, self.lynx, self.rfid)
        self.assertEqual(path, os.path.join(self.rfid, "002-1-01.lif"))
        self.assertEqual(sorted(os.listdir(self.lynx)), ["RFID"])

    def test_unchanged_content_is_not_rewritten(self):
        self.mgr.write_merged_lif(self.key, self.lynx, self.rfid)
        before = os.stat(os.path.join(self.rfid, "002-1-01.lif")).st_mtime_ns
        self.mgr.write_merged_lif(self.key, self.lynx, self.rfid)
        self.assertEqual(os.stat(os.path.join(self.rfid, "002-1-01.lif")).st_mtime_ns, before)

    def test_picks_up_finishlynx_file(self):
        self.mgr.write_merged_lif(self.key, self.lynx, self.rfid)
        self.write_fl(FL_TEXT)
        self.mgr.write_merged_lif(self.key, self.lynx, self.rfid)
        _, rows = rows_of(self.read())
        self.assertIn("99", [r[1] for r in rows])

    def test_old_finishlynx_file_from_an_earlier_meet_is_ignored(self):
        self.write_fl(FL_TEXT, saved=GUN - timedelta(days=3))
        self.mgr.write_merged_lif(self.key, self.lynx, self.rfid)
        _, rows = rows_of(self.read())
        self.assertNotIn("99", [r[1] for r in rows])

    def test_half_written_finishlynx_file_keeps_the_last_good_copy(self):
        self.write_fl(FL_TEXT)
        self.mgr.write_merged_lif(self.key, self.lynx, self.rfid)
        good = self.read()
        self.write_fl(FL_TEXT[:120])                          # caught mid-write
        self.mgr.write_merged_lif(self.key, self.lynx, self.rfid)
        self.assertEqual(self.read(), good)

    def test_empty_pane_writes_nothing(self):
        key = self.mgr.create_race("Boys 5K", event=1, gender="M")
        self.assertIsNone(self.mgr.write_merged_lif(key, self.lynx, self.rfid))
        self.assertFalse(os.path.exists(os.path.join(self.rfid, "001-1-01.lif")))


class TestCloseMeetWipe(unittest.TestCase):

    def test_archives_then_removes_only_our_lif_files(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        rfid, archive = os.path.join(d, "RFID"), os.path.join(d, "closed")
        os.makedirs(rfid)
        for n in ("002-1-01.lif", "lynx.evt", "lynx.ppl", "lynx.sch"):
            with open(os.path.join(rfid, n), "w") as f:
                f.write(n)
        self.assertEqual(wipe_merged_lifs(rfid, archive), ["002-1-01.lif"])
        self.assertEqual(sorted(os.listdir(rfid)), ["lynx.evt", "lynx.ppl", "lynx.sch"])
        self.assertTrue(os.path.exists(os.path.join(archive, "rfid-lif", "002-1-01.lif")))

    def test_missing_folder_is_fine(self):
        self.assertEqual(wipe_merged_lifs(os.path.join(tempfile.gettempdir(), "no-such-rfid-dir")), [])


class TestSaveAsEndpoint(unittest.TestCase):

    def setUp(self):
        self.lynx = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.lynx, True)
        self.mgr, self.key = race_with_splits()
        web._application = SimpleNamespace(race_manager=self.mgr, config=None,
                                           lif_dirs=lambda: (self.lynx, os.path.join(self.lynx, "RFID")))
        self.addCleanup(setattr, web, "_application", None)
        self.client = web.app.test_client()

    def test_download_is_the_merged_file(self):
        r = self.client.get(f"/api/races/{self.key}/lif")
        self.assertEqual(r.status_code, 200)
        self.assertIn("002-1-01.lif", r.headers["Content-Disposition"])
        self.assertEqual(r.data.decode("cp1252"), self.mgr.lif_content(self.key))

    def test_finish_only(self):
        r = self.client.get(f"/api/races/{self.key}/lif?splits=0")
        _, rows = rows_of(r.data.decode("cp1252"))
        self.assertTrue(all(row[10] == "" for row in rows))

    def test_unknown_race(self):
        self.assertEqual(self.client.get("/api/races/999/lif").status_code, 404)


if __name__ == "__main__":
    unittest.main()
