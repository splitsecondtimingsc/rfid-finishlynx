"""Close meet into the FinishLynx results folder (meet_folder.py, 2026-10-05):
'<date> <meet name>' made there, FinishLynx's files + the RFID folder copied
in and checked, originals deleted only after every copy passed; full disk,
bad copies, interruptions and locked files never lose a file and a second
Close meet finishes the job.

Hardware- and network-free: python -m unittest tests.test_meet_folder"""

import os
import shutil
import unittest
from datetime import timedelta
from unittest import mock

from rfid_finishlynx import meet_folder
from tests.test_meet_api import GUN, MeetApiBase

FOLDER = "2026-10-10 Test XC"


class CloseIntoResultsFolder(MeetApiBase):

    def setUp(self):
        super().setUp()
        self.import_fixture()
        self.assign_all()
        pane = self.make_panes()[(1, 1, 1)]
        race = self.svc.meet.race(1, 1, 1)
        self.mgr.set_gun(pane, GUN)
        for i, e in enumerate(race.entries[:5]):
            self.mgr.ingest(self.svc.meet.athlete(e.athlete_id).bib, GUN + timedelta(minutes=18, seconds=i), "FINISH")
        # FinishLynx's own files at the top (flat), our merged .lif in RFID;
        # an earlier closed meet and a hand-made folder are left alone.
        self.files = {"001-1-01.lif": b"fl results", "002-1-01.lif": b"\x00" * 5000,
                      "RFID/001-1-01.lif": b"merged"}
        os.makedirs(os.path.join(self.lynx, "Notes"))
        with open(os.path.join(self.lynx, "Notes", "keep.txt"), "wb") as f:
            f.write(b"mine")
        for rel, data in self.files.items():
            path = os.path.join(self.lynx, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        self.files.update({f"RFID/{n}": self.read(os.path.join(self.rfid, n))
                           for n in os.listdir(self.rfid) if n.lower().startswith("lynx.")})
        self.old = meet_folder.create(self.lynx, "2026-09-01 Old Meet", "Old Meet")
        meet_folder.write_marker(self.old, {"state": "done", "files": {}})
        with open(os.path.join(self.old, "002-1-01.lif"), "wb") as f:
            f.write(b"old")
        self.dest = os.path.join(self.lynx, FOLDER)

    @staticmethod
    def read(path):
        with open(path, "rb") as f:
            return f.read()

    def left_in_lynx(self):
        out = []
        for d, _, names in os.walk(self.lynx):
            if d.startswith((self.old, self.dest, os.path.join(self.lynx, "Notes"))):
                continue
            out += [os.path.relpath(os.path.join(d, n), self.lynx).replace(os.sep, "/") for n in names]
        return sorted(out)

    def assert_all_copied(self):
        for rel, data in self.files.items():
            self.assertEqual(self.read(os.path.join(self.dest, rel)), data, rel)

    def test_plan(self):
        d = self.get("/api/meet/close/plan").get_json()
        self.assertEqual(d["folder"], self.dest)
        self.assertFalse(d["resume"])
        self.assertFalse(d["finishlynx_running"])
        self.assertEqual(d["files"], 2)
        self.assertEqual(d["rfid_files"], len([r for r in self.files if r.startswith("RFID/")]))

    def test_close_copies_checks_then_clears(self):
        d = self.post("/api/meet/close")
        self.assertTrue(d["success"], d)
        self.assertEqual(d["archive_dir"], self.dest)
        self.assertEqual(d["files_moved"], len(self.files))
        self.assert_all_copied()
        for name in ("meet-results.csv", "races-state.json", os.path.join("meet", "meet.json")):
            self.assertTrue(os.path.isfile(os.path.join(self.dest, "Middleware", name)), name)
        self.assertEqual(self.left_in_lynx(), [])
        self.assertEqual(sorted(os.listdir(self.lynx)), sorted(["RFID", FOLDER, "2026-09-01 Old Meet", "Notes"]))
        self.assertEqual(self.read(os.path.join(self.lynx, "Notes", "keep.txt")), b"mine")
        self.assertEqual(self.read(os.path.join(self.old, "002-1-01.lif")), b"old")
        marker = meet_folder.read_marker(self.dest)
        self.assertEqual(marker["state"], "done")
        self.assertEqual(set(marker["files"]), set(self.files))
        self.assertEqual(self.mgr.races, {})
        self.assertEqual(self.get("/api/meet").get_json(), {"active": False})
        # the next close of the same name does not reuse the folder
        meet_folder.create(self.lynx, FOLDER, "x")
        self.assertTrue(os.path.isdir(self.dest + " (2)"))

    def test_finishlynx_open_blocks_close(self):
        with mock.patch("rfid_finishlynx.fl_window.running", return_value=True):
            self.assertTrue(self.get("/api/meet/close/plan").get_json()["finishlynx_running"])
            d = self.post("/api/meet/close", status=409)
        self.assertIn("FinishLynx is still open", d["error"])
        self.assertFalse(os.path.exists(self.dest))
        self.assertTrue(self.mgr.races)
        self.assertEqual(len(self.left_in_lynx()), len(self.files))

    def test_full_disk_deletes_nothing_and_retry_finishes(self):
        with mock.patch("rfid_finishlynx.meet_folder.shutil.disk_usage",
                        return_value=shutil._ntuple_diskusage(10 ** 9, 10 ** 9, 1000)):
            d = self.post("/api/meet/close", status=500)
        self.assertIn("not enough free disk space", d["error"])
        self.assertTrue(d["cleared"])
        self.assertEqual(len(self.left_in_lynx()), len(self.files))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "Middleware", "races-state.json")))
        self.assertTrue(self.get("/api/meet/close/plan").get_json()["resume"])

        # a new meet opened meanwhile is not closed into the old folder
        self.import_fixture(name="Next XC", date="2026-10-17", meet_id=761227)    # another MileSplit meet
        d = self.post("/api/meet/close")
        self.assertTrue(d["resumed"])
        self.assertEqual(self.svc.meet.meet.name, "Next XC")
        self.assertEqual(self.left_in_lynx(), [])
        self.assertEqual(meet_folder.read_marker(self.dest)["state"], "done")
        for rel in ("001-1-01.lif", "002-1-01.lif", "RFID/001-1-01.lif"):
            self.assertEqual(self.read(os.path.join(self.dest, rel)), self.files[rel])

    def test_bad_copy_deletes_nothing(self):
        with mock.patch("rfid_finishlynx.meet_folder._sha256", return_value="0" * 64):
            d = self.post("/api/meet/close", status=500)
        self.assertIn("does not match the original", d["error"])
        self.assertEqual(len(self.left_in_lynx()), len(self.files))
        self.assertEqual(meet_folder.read_marker(self.dest)["state"], "copying")
        self.assertTrue(self.post("/api/meet/close")["success"])
        self.assert_all_copied()
        self.assertEqual(self.left_in_lynx(), [])

    def test_interrupted_copy_leaves_no_part_files(self):
        real = meet_folder.copy_verified
        calls = []

        def failing(src, dst):
            calls.append(src)
            if len(calls) == 2:
                with open(dst + meet_folder.PART, "wb") as f:
                    f.write(b"half")
                os.remove(dst + meet_folder.PART)
                raise meet_folder.CloseError("could not copy: [Errno 28] No space left on device")
            return real(src, dst)

        with mock.patch("rfid_finishlynx.meet_folder.copy_verified", side_effect=failing):
            self.post("/api/meet/close", status=500)
        self.assertEqual(len(self.left_in_lynx()), len(self.files))
        parts = [n for _, _, ns in os.walk(self.dest) for n in ns if n.endswith(meet_folder.PART)]
        self.assertEqual(parts, [])
        self.assertTrue(self.post("/api/meet/close")["success"])
        self.assert_all_copied()

    def test_copy_failure_removes_part_file(self):
        src = os.path.join(self.lynx, "001-1-01.lif")
        dst = os.path.join(self.dest, "x.lif")
        os.makedirs(self.dest)
        with mock.patch("rfid_finishlynx.meet_folder.os.fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(meet_folder.CloseError):
                meet_folder.copy_verified(src, dst)
        self.assertEqual(os.listdir(self.dest), [])

    def test_locked_original_is_kept_and_retry_finishes(self):
        real = os.remove
        locked = os.path.join(self.lynx, "001-1-01.lif")

        def remove(path):
            if path == locked:
                raise PermissionError(13, "The process cannot access the file")
            return real(path)

        with mock.patch("rfid_finishlynx.meet_folder.os.remove", side_effect=remove):
            d = self.post("/api/meet/close", status=500)
        self.assertIn("1 original(s) are still in", d["error"])
        self.assertEqual(self.left_in_lynx(), ["001-1-01.lif"])
        self.assertEqual(meet_folder.read_marker(self.dest)["state"], "deleting")
        self.assert_all_copied()
        self.assertTrue(self.post("/api/meet/close")["success"])
        self.assertEqual(self.left_in_lynx(), [])

    def test_original_changed_after_copy_is_kept(self):
        real = meet_folder.copy_verified
        changed = os.path.join(self.lynx, "001-1-01.lif")

        def then_write(src, dst):
            entry = real(src, dst)
            if src == changed:
                with open(src, "ab") as f:
                    f.write(b" + late write")
            return entry

        with mock.patch("rfid_finishlynx.meet_folder.copy_verified", side_effect=then_write):
            d = self.post("/api/meet/close", status=500)
        self.assertIn("changed after it was copied", d["error"])
        self.assertEqual(self.read(changed), b"fl results + late write")
        self.assertTrue(self.post("/api/meet/close")["success"])
        self.assertEqual(self.read(os.path.join(self.dest, "001-1-01.lif")), b"fl results + late write")

    # ------------------------------------------------------------ reopen

    def close(self):
        self.meet_id = self.svc.meet.meet.id
        self.reads = len(self.mgr.reads)
        self.assertTrue(self.post("/api/meet/close")["success"])

    def assert_reopened(self, d):
        self.assertEqual(d["reopened"]["folder"], self.dest)
        self.assertEqual(d["reopened"]["files"], len(self.files))
        self.assertEqual(len(self.mgr.reads), self.reads)
        self.assertTrue(self.mgr.races)
        self.assertEqual(self.svc.meet.meet.id, self.meet_id)
        for rel, data in self.files.items():
            if not rel.startswith("RFID/lynx."):          # rewritten on open
                self.assertEqual(self.read(os.path.join(self.lynx, rel)), data, rel)

    def test_saved_meets_list_reopens_from_the_meet_folder(self):
        self.close()
        listed = {m["id"]: m for m in self.get("/api/meet/list").get_json()["meets"]}
        self.assertEqual(listed[self.meet_id]["closed_to"], self.dest)
        self.assert_reopened(self.post("/api/meet/open", {"id": self.meet_id}))

        # fix a missed kid, close again: saved back into the same folder
        with open(os.path.join(self.lynx, "001-1-01.lif"), "wb") as f:
            f.write(b"fl results fixed")
        pane = next(iter(self.mgr.races))
        self.mgr.ingest("1999", GUN + timedelta(minutes=30), "FINISH")
        plan = self.get("/api/meet/close/plan").get_json()
        self.assertEqual((plan["folder"], plan["again"]), (self.dest, True))
        d = self.post("/api/meet/close")
        self.assertTrue(d["success"], d)
        self.assertEqual(d["archive_dir"], self.dest)
        self.assertFalse(os.path.exists(self.dest + " (2)"))
        self.assertEqual(self.read(os.path.join(self.dest, "001-1-01.lif")), b"fl results fixed")
        self.assertEqual(self.left_in_lynx(), [])
        with open(os.path.join(self.dest, "Middleware", "races-state.json")) as f:
            self.assertIn('"1999"', f.read())
        self.assertTrue(pane)

    def test_open_file_inside_the_meet_folder_reopens(self):
        self.close()
        path = os.path.join(self.dest, "Middleware", "meet", "meet.json")
        self.assert_reopened(self.post("/api/meet/open-file", {"path": path}))

    def test_reopen_refused_while_another_meet_is_running(self):
        self.close()
        with open(os.path.join(self.lynx, "003-1-01.lif"), "wb") as f:
            f.write(b"next meet")
        d = self.post("/api/meet/open", {"id": self.meet_id}, status=409)
        self.assertIn("results folder already has 1 file(s)", d["error"])
        self.assertIn("003-1-01.lif", d["error"])                 # every file is named
        os.remove(os.path.join(self.lynx, "003-1-01.lif"))
        # a race card in use (a stray held read alone no longer blocks, break test 2026-10-13)
        self.mgr.set_gun(self.mgr.create_race("Next meet", event=9), GUN)
        d = self.post("/api/meet/open", {"id": self.meet_id}, status=409)
        self.assertIn("close the current meet first", d["error"])
        self.assertEqual(self.left_in_lynx(), [])
        with mock.patch("rfid_finishlynx.fl_window.running", return_value=True):
            self.mgr.close_meet()
            self.post("/api/meet/open", {"id": self.meet_id}, status=409)
        self.assertEqual(self.left_in_lynx(), [])

    def test_failed_copy_back_leaves_results_folder_empty(self):
        self.close()
        real = meet_folder.copy_verified
        calls = []

        def failing(src, dst):
            calls.append(src)
            if len(calls) == 3:
                raise meet_folder.CloseError("could not copy: disk full")
            return real(src, dst)

        with mock.patch("rfid_finishlynx.meet_folder.copy_verified", side_effect=failing):
            self.post("/api/meet/open", {"id": self.meet_id}, status=500)
        self.assertEqual(self.left_in_lynx(), [])
        self.assertEqual(self.mgr.reads, {})
        self.assertEqual(self.get("/api/meet").get_json(), {"active": False})

    def test_race_with_reads_gone_cannot_be_reopened(self):
        pane = self.make_panes()[(1, 1, 1)]
        race = self.svc.meet.race(1, 1, 1)
        for e in race.entries[5:]:
            self.post("/api/meet/races/1-1-1/marks", {"bib": self.svc.meet.athlete(e.athlete_id).bib, "mark": "DNS"})
        self.post("/api/meet/races/1-1-1/final")
        self.assertTrue(pane)
        self.close()
        shutil.rmtree(self.dest)                     # meet folder gone: console copy only
        d = self.post("/api/meet/open", {"id": self.meet_id}, status=409)
        self.assertEqual(d["folder_missing"], self.dest)
        self.post("/api/meet/open", {"id": self.meet_id, "console_copy": True})
        self.assertEqual(self.svc.meet.race(1, 1, 1).status, "final")
        self.assertTrue(self.svc.file_only)              # the console's copy: meet file only
        d = self.post("/api/meet/races/1-1-1/reopen", status=409)
        self.assertIn("opened without FinishLynx", d["error"])

    # ------------------------------------------------ open without FinishLynx

    def test_open_plan_lists_what_stops_finishlynx(self):
        """The Open dialog's choice (2026-10-05): with FinishLynx needs an
        empty results folder (every file there is named) and no FinishLynx
        running; the meet file alone needs neither."""
        self.close()
        plan = self.get(f"/api/meet/open/plan?id={self.meet_id}").get_json()
        self.assertTrue(plan["closed"])
        self.assertTrue(plan["finishlynx_ok"], plan)
        self.assertEqual(plan["files"], len(self.files))
        for name in ("003-1-01.lif", "004-1-01.lif"):
            with open(os.path.join(self.lynx, name), "wb") as f:
                f.write(b"the next meet")
        with mock.patch("rfid_finishlynx.fl_window.running", return_value=True):
            plan = self.get(f"/api/meet/open/plan?id={self.meet_id}").get_json()
        self.assertFalse(plan["finishlynx_ok"])
        self.assertEqual(plan["loose"], ["003-1-01.lif", "004-1-01.lif"])
        self.assertEqual(len(plan["finishlynx_why"]), 2)             # files + FinishLynx open
        self.assertTrue(plan["file_only_ok"])
        path = os.path.join(self.dest, "Middleware", "meet", "meet.json")
        by_path = self.get(f"/api/meet/open/plan?path={path}").get_json()
        self.assertEqual(by_path["folder"], self.dest)
        self.assertFalse(self.get("/api/meet/open/plan?id=nope").get_json()["closed"])

    def test_open_without_finishlynx_moves_nothing_and_saves_back(self):
        self.close()
        state = os.path.join(self.dest, "Middleware", "races-state.json")
        state_before = self.read(state)
        with open(os.path.join(self.lynx, "003-1-01.lif"), "wb") as f:
            f.write(b"the next meet")                                # another meet's file
        d = self.post("/api/meet/open", {"id": self.meet_id, "mode": "file"})
        self.assertTrue(d["reopened"]["file_only"])
        self.assertEqual(d["reopened"]["files"], 0)
        self.assertTrue(d["meet"]["file_only"])
        self.assertEqual(d["meet"]["closed_to"], self.dest)
        self.assertEqual(d["meet"]["start_lists"]["skipped"], "file only")
        self.assertEqual(self.left_in_lynx(), ["003-1-01.lif"])      # nothing copied, no start lists
        self.assertEqual((self.mgr.races, self.mgr.reads), ({}, {}))  # no panes made, no reads
        # print / edit yes, change results no
        self.post("/api/meet/info", {"host": "Ridge High School (fixed)"})
        self.assertIn("opened without FinishLynx",
                      self.post("/api/meet/races/1-1-1/final", status=409)["error"])
        plan = self.get("/api/meet/close/plan").get_json()
        self.assertEqual((plan["file_only"], plan["folder"]), (True, self.dest))
        d = self.post("/api/meet/close")
        self.assertTrue(d["success"], d)
        self.assertTrue(d["file_only"])
        self.assertEqual(self.get("/api/meet").get_json(), {"active": False})
        # the meet file went back into the meet folder; nothing else changed
        saved = os.path.join(self.dest, "Middleware", "meet", "meet.json")
        self.assertIn("Ridge High School (fixed)", self.read(saved).decode("utf-8"))
        self.assertEqual(self.read(state), state_before)
        self.assertEqual(self.left_in_lynx(), ["003-1-01.lif"])
        self.assertEqual(meet_folder.read_marker(self.dest)["state"], "done")
        # and opening it with FinishLynx later brings the edit back
        os.remove(os.path.join(self.lynx, "003-1-01.lif"))
        self.assert_reopened(self.post("/api/meet/open", {"id": self.meet_id}))
        self.assertEqual(self.svc.meet.meet.host, "Ridge High School (fixed)")
        self.assertFalse(self.svc.file_only)

    def test_leaving_a_file_only_meet_saves_it_back(self):
        """Opening another meet (not Save & close) must not lose the edits:
        the next reopen copies the meet folder's file over the console's."""
        self.close()
        self.post("/api/meet/open", {"id": self.meet_id, "mode": "file"})
        self.post("/api/meet/info", {"location": "Edited before switching"})
        self.post("/api/meet/new", {"name": "Next Meet", "date": "2026-10-17"})
        self.assertFalse(self.svc.file_only)
        saved = os.path.join(self.dest, "Middleware", "meet", "meet.json")
        self.assertIn("Edited before switching", self.read(saved).decode("utf-8"))

    def test_file_only_survives_a_restart(self):
        self.close()
        self.post("/api/meet/open-file", {"path": os.path.join(self.dest, "Middleware", "meet", "meet.json"),
                                          "mode": "file"})
        self.assertTrue(self.svc.file_only)
        again = self.make_service()                       # console restarted
        self.assertEqual(again.meet.meet.id, self.meet_id)
        self.assertTrue(again.file_only)
        self.assertEqual(self.left_in_lynx(), [])

    def test_bad_mode_refused(self):
        self.close()
        self.post("/api/meet/open", {"id": self.meet_id, "mode": "both"}, status=400)

    def test_fix_missed_kid_after_close(self):
        """Final -> close -> reopen meet -> reopen race -> add the missed
        kid's read -> final -> close -> reopen: the kid is in the official
        results, from the same meet folder."""
        race = self.svc.meet.race(1, 1, 1)
        missed = self.svc.meet.athlete(race.entries[5].athlete_id).bib
        self.post("/api/meet/races/1-1-1/final")

        def official():
            d = self.get("/api/meet/races/1-1-1/results").get_json()
            self.assertTrue(d["official"])
            return {str(f["bib"]): f for f in d["results"]["finishers"]}

        self.assertNotEqual(official()[missed]["status"], "finished")
        self.close()
        self.post("/api/meet/open", {"id": self.meet_id})
        self.post("/api/meet/races/1-1-1/reopen")
        self.mgr.ingest(missed, GUN + timedelta(minutes=18, seconds=30), "FINISH")
        self.post("/api/meet/races/1-1-1/final")
        self.assertEqual(official()[missed]["status"], "finished")
        self.assertTrue(self.post("/api/meet/close")["success"])
        self.assertFalse(os.path.exists(self.dest + " (2)"))

        self.post("/api/meet/open", {"id": self.meet_id})
        self.assertEqual(official()[missed]["status"], "finished")
        self.assertEqual(sum(f["status"] == "finished" for f in official().values()), 6)

    def test_moved_meet_folder(self):
        self.close()
        nas = os.path.join(self.tmp, "NAS", "XC 2026")
        os.makedirs(os.path.dirname(nas))
        shutil.move(self.dest, nas)
        d = self.post("/api/meet/open", {"id": self.meet_id}, status=409)
        self.assertIn("Browse", d["error"])
        self.assertEqual(self.left_in_lynx(), [])

        meet_file = os.path.join(nas, FOLDER + meet_folder.MARKER_SUFFIX)
        self.assertTrue(os.path.isfile(meet_file))
        d = self.post("/api/meet/open-file", {"path": meet_file})
        self.assertEqual(d["reopened"]["folder"], nas)
        self.assertEqual(len(self.mgr.reads), self.reads)
        self.assertEqual(self.svc.archived_folder(self.meet_id), nas)

        # closing again makes the meet folder in the results folder again;
        # the moved copy is not touched
        before = sorted(os.listdir(nas))
        self.assertTrue(self.post("/api/meet/close")["success"])
        self.assert_all_copied()
        self.assertEqual(sorted(os.listdir(nas)), before)
        self.assertEqual(self.svc.archived_folder(self.meet_id), self.dest)

    def test_meet_file_name(self):
        self.close()
        self.assertEqual(sorted(n for n in os.listdir(self.dest) if n.endswith(".json")),
                         [FOLDER + " - MEET FILE.json"])
        self.assertEqual(os.path.basename(meet_folder.create(self.lynx, FOLDER, "x")),
                         FOLDER + " (2)")
        self.assertTrue(os.path.isfile(os.path.join(self.dest + " (2)", FOLDER + " (2) - MEET FILE.json")))
        # a renamed folder is still a meet folder and still reopens
        renamed = os.path.join(self.lynx, "Crawford XC")
        os.rename(self.dest, renamed)
        self.assertTrue(meet_folder.is_meet_folder(renamed))
        d = self.post("/api/meet/open-file", {"path": os.path.join(renamed, FOLDER + " - MEET FILE.json")})
        self.assertEqual(d["reopened"]["folder"], renamed)

    def test_preview_uses_closed_meets(self):
        self.app.hardware_allowed = False
        d = self.post("/api/meet/close")
        self.assertIn("closed-meets", d["archive_dir"])
        self.assertEqual(len(self.left_in_lynx()), len(self.files))
        self.assertIsNone(self.get("/api/meet/close/plan").get_json()["folder"])


class FolderName(unittest.TestCase):

    def test_windows_safe(self):
        self.assertEqual(meet_folder.folder_name("2026-10-08", 'Jim Crawford Invitational (Crescent High)'),
                         "2026-10-08 Jim Crawford Invitational (Crescent High)")
        self.assertEqual(meet_folder.folder_name("2026-10-08", 'A/B: "XC"?. '), '2026-10-08 A-B- -XC--')
        self.assertEqual(meet_folder.folder_name("", ""), "Meet")


if __name__ == "__main__":
    unittest.main()
