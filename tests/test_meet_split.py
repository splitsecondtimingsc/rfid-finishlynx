"""2026-10-10: a meet in a shared folder (Google Drive) split by section, so
an edit re-uploads only the part it touched; rebuilt only when every file is
there and is the one the manifest lists; a meet rebuilt from a split folder
or a single .json / .json.gz. Hardware-free:
python -m unittest tests.test_meet_split
"""
import gzip
import json
import os
import shutil
import tempfile
import unittest

from rfid_finishlynx import meet_split as sp
from rfid_finishlynx import meet_sync as ms
from rfid_finishlynx.meet.model import MeetStore
from tests.test_meet_sync import make_meet


def snap_of(store_root, meet_id):
    with open(os.path.join(store_root, meet_id, "meet.json"), encoding="utf-8") as f:
        return json.load(f)


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="meetsplit-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store")
        os.makedirs(self.store)
        self.mid = make_meet(self.store)
        self.snap = snap_of(self.store, self.mid)
        self.shared = os.path.join(self.tmp, "My Drive", "Meet Backups")
        os.makedirs(self.shared)


class TestSplitJoin(Base):

    def test_round_trip_is_exact(self):
        files = sp.split(self.snap)
        self.assertEqual(ms.digest(sp.join(files)), ms.digest(self.snap))
        self.assertEqual(ms.pack(sp.join(files)), ms.pack(self.snap))
        names = sorted(files)
        self.assertIn("manifest.json", names)
        self.assertIn("meet.json.gz", names)
        self.assertIn("teams.json.gz", names)
        self.assertTrue(any(n.startswith("athletes/") for n in names))
        self.assertEqual(len([n for n in names if n.endswith("/race.json.gz")]), len(self.snap["races"]))
        self.assertEqual(len([n for n in names if n.endswith("/entries.json.gz")]), len(self.snap["races"]))

    def test_a_mark_does_not_send_the_entries_again(self):
        before = sp.split(self.snap)
        self.snap["races"][0].setdefault("marks", {})["101"] = "DNF"
        after = sp.split(self.snap)
        changed = sorted(n for n in after if before.get(n) != after[n])
        self.assertEqual(len(changed), 2)
        self.assertTrue(changed[1].endswith("/race.json.gz"), changed)

    def test_one_bib_touches_one_team_file(self):
        before = sp.split(self.snap)
        a = self.snap["athletes"][5]
        a["bib"] = "9999"
        after = sp.split(self.snap)
        changed = sorted(n for n in after if before.get(n) != after[n])
        team_file = next(n for n in after if n.startswith("athletes/") and
                         any(x["id"] == a["id"] for x in json.loads(gzip.decompress(after[n]))))
        self.assertEqual(changed, sorted(["manifest.json", team_file]))

    def test_any_athlete_order_comes_back(self):
        self.snap["athletes"].reverse()
        self.assertEqual(ms.pack(sp.join(sp.split(self.snap))), ms.pack(self.snap))

    def test_athletes_without_ids_stay_in_one_file(self):
        self.snap["athletes"][0]["id"] = ""
        files = sp.split(self.snap)
        self.assertIn("athletes/_all.json.gz", files)
        self.assertEqual(ms.pack(sp.join(files)), ms.pack(self.snap))

    def test_half_synced_is_not_a_meet(self):
        files = sp.split(self.snap)
        race = next(n for n in files if n.endswith("/entries.json.gz"))
        missing = dict(files)
        del missing[race]
        with self.assertRaises(sp.Incomplete):
            sp.join(missing)
        old = dict(files)
        old[race] = gzip.compress(b"[]", mtime=0)          # an older copy, not the one listed
        with self.assertRaises(sp.Incomplete):
            sp.join(old)
        with self.assertRaises(sp.Incomplete):
            sp.join({k: v for k, v in files.items() if k != "manifest.json"})


class TestFolderTransport(Base):

    def test_only_changed_files_are_written(self):
        t = ms.FolderTransport(self.shared)
        t.put(self.mid, "G15FL", ms.pack(self.snap))
        folder = os.path.join(self.shared, self.mid, "G15FL")
        self.assertTrue(sp.is_split_folder(folder))
        self.snap["athletes"][3]["bib"] = "4242"
        written = sp.write_folder(folder, sp.split(self.snap))
        self.assertEqual(len(written), 2)                  # one team file + the manifest
        self.assertEqual(written[-1], "manifest.json")    # the manifest goes last
        self.assertEqual(sp.write_folder(folder, sp.split(self.snap)), [])

    def test_reads_back_and_replaces_the_single_file(self):
        mdir = os.path.join(self.shared, self.mid)
        os.makedirs(mdir)
        with open(os.path.join(mdir, "G15FL.json.gz"), "wb") as f:
            f.write(ms.pack(self.snap))
        t = ms.FolderTransport(self.shared)
        got = t.fetch()[self.mid]["G15FL"]
        self.assertEqual(ms.unpack(got), self.snap)        # a machine not updated yet: still read
        t.put(self.mid, "G15FL", ms.pack(self.snap))
        self.assertEqual(os.listdir(mdir), ["G15FL"])      # the folder replaces the single file
        got = t.fetch()[self.mid]["G15FL"]
        self.assertEqual(ms.unpack(got), self.snap)
        self.assertFalse(got.single)

    def test_half_synced_folder_waits(self):
        t = ms.FolderTransport(self.shared)
        t.put(self.mid, "FEDORA", ms.pack(self.snap))
        folder = os.path.join(self.shared, self.mid, "FEDORA")
        race = next(n for n in os.listdir(os.path.join(folder, "races")))
        os.remove(os.path.join(folder, "races", race, "entries.json.gz"))   # Drive hasn't brought it down yet
        self.assertNotIn("FEDORA", t.fetch().get(self.mid, {}))
        t.put(self.mid, "FEDORA", ms.pack(self.snap))       # ... now it has
        self.assertIn("FEDORA", t.fetch()[self.mid])

    def test_drive_conflict_copies_are_ignored_and_tidied(self):
        t = ms.FolderTransport(self.shared)
        t.put(self.mid, "G15FL", ms.pack(self.snap))
        folder = os.path.join(self.shared, self.mid, "G15FL")
        stray = os.path.join(folder, "races", os.listdir(os.path.join(folder, "races"))[0], "race (1).json.gz")
        with open(stray, "wb") as f:
            f.write(b"junk")
        self.assertEqual(ms.unpack(t.fetch()[self.mid]["G15FL"]), self.snap)
        t.put(self.mid, "G15FL", ms.pack(self.snap))
        self.assertFalse(os.path.exists(stray))

    def test_two_machines_merge_through_split_folders(self):
        b_store = os.path.join(self.tmp, "b")
        os.makedirs(b_store)
        a = ms.MeetSync(ms.DirStore(self.store), ms.FolderTransport(self.shared), "G15FL",
                        os.path.join(self.tmp, "a-state"))
        b = ms.MeetSync(ms.DirStore(b_store), ms.FolderTransport(self.shared), "TRAILER",
                        os.path.join(self.tmp, "b-state"))
        a.sync_once()
        b.sync_once()
        self.assertEqual(snap_of(b_store, self.mid)["athletes"], self.snap["athletes"])
        store = MeetStore(b_store)
        m = store.load(self.mid)
        m.athlete("a1").bib = "777"
        store.save(m)
        b.sync_once()
        a.sync_once()
        self.assertEqual(MeetStore(self.store).load(self.mid).athlete("a1").bib, "777")


class TestRebuild(Base):

    def test_read_any(self):
        t = ms.FolderTransport(self.shared)
        t.put(self.mid, "G15FL", ms.pack(self.snap))
        folder = os.path.join(self.shared, self.mid, "G15FL")
        for path in (folder, os.path.join(folder, "manifest.json")):
            got = sp.read_any(path)
            got.pop("sync", None)
            self.assertEqual(ms.digest(got), ms.digest(self.snap))
        gz = os.path.join(self.tmp, "G15FL.json.gz")
        with open(gz, "wb") as f:
            f.write(ms.pack(self.snap))
        self.assertEqual(sp.read_any(gz), self.snap)
        self.assertEqual(sp.read_any(os.path.join(self.store, self.mid)), self.snap)

    def test_cli_rebuild_and_split(self):
        folder = os.path.join(self.tmp, "split")
        self.assertEqual(sp.main(["split", os.path.join(self.store, self.mid, "meet.json"), folder]), 0)
        out = os.path.join(self.tmp, "rebuilt.json")
        self.assertEqual(sp.main(["rebuild", folder, "-o", out]), 0)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(ms.digest(json.load(f)), ms.digest(self.snap))
        os.remove(os.path.join(folder, "teams.json.gz"))
        self.assertEqual(sp.main(["rebuild", folder, "-o", out]), 2)      # not complete

    def test_open_another_rebuilds_from_a_manifest(self):
        from rfid_finishlynx.meet_service import MeetService
        t = ms.FolderTransport(self.shared)
        t.put(self.mid, "G15FL", ms.pack(self.snap))
        other = os.path.join(self.tmp, "other-console")
        os.makedirs(other)
        svc = MeetService(other)
        svc.open_path(os.path.join(self.shared, self.mid, "G15FL", "manifest.json"))
        self.assertEqual(svc.meet.meet.id, self.mid)
        self.assertEqual(len(svc.meet.athletes), len(self.snap["athletes"]))
        svc.open_path(os.path.join(self.shared, self.mid, "G15FL"))      # the folder: a copy, nothing overwritten
        self.assertEqual(svc.meet.meet.id, self.mid + "-copy")


class TestSnapshot(Base):
    """What the sync really writes: {"meet": meet.json, "files": pulls...}."""

    def setUp(self):
        super().setUp()
        self.whole = ms.read_snapshot(self.store, self.mid)
        self.whole["sync"] = {"machine": "G15FL", "merged": {}}

    def test_round_trip_and_layout(self):
        files = sp.split(self.whole)
        self.assertEqual(ms.pack(sp.join(files)), ms.pack(self.whole))
        self.assertIn("snapshot.json.gz", files)
        self.assertIn("files/pulls/entries.json.gz", files)
        self.assertTrue(any(n.startswith("athletes/") for n in files))
        self.assertTrue(any(n.endswith("/entries.json.gz") and n.startswith("races/") for n in files))

    def test_a_bib_change_leaves_the_pulls_alone(self):
        before = sp.split(self.whole)
        self.whole["meet"]["athletes"][2]["bib"] = "5150"
        after = sp.split(self.whole)
        changed = sorted(n for n in after if before.get(n) != after[n])
        self.assertEqual(len(changed), 2, changed)                 # the team's file + the manifest
        self.assertFalse(any(n.startswith("files/") for n in changed))

    def test_sync_engine_writes_and_reads_it(self):
        a = ms.MeetSync(ms.DirStore(self.store), ms.FolderTransport(self.shared), "G15FL",
                        os.path.join(self.tmp, "a-state"))
        a.sync_once()
        folder = os.path.join(self.shared, self.mid, "G15FL")
        self.assertTrue(os.path.isfile(os.path.join(folder, "files", "pulls", "entries.json.gz")))
        got = ms.FolderTransport(self.shared).fetch()[self.mid]["G15FL"]
        mine = ms.read_snapshot(self.store, self.mid)
        self.assertEqual(ms.digest(ms.unpack(got)), ms.digest(mine))

    def test_open_another_brings_the_pulls(self):
        from rfid_finishlynx.meet_service import MeetService
        a = ms.MeetSync(ms.DirStore(self.store), ms.FolderTransport(self.shared), "G15FL",
                        os.path.join(self.tmp, "a-state"))
        a.sync_once()
        other = os.path.join(self.tmp, "other-console")
        os.makedirs(other)
        svc = MeetService(other)
        svc.open_path(os.path.join(self.shared, self.mid, "G15FL", "manifest.json"))
        self.assertTrue(os.path.isfile(os.path.join(other, self.mid, "pulls", "entries.json")))
        self.assertEqual(len(svc.meet.athletes), len(self.snap["athletes"]))

    def test_cli_rebuild_to_a_folder(self):
        ms.FolderTransport(self.shared).put(self.mid, "G15FL", ms.pack(self.whole))
        out = os.path.join(self.tmp, "rebuilt", self.mid)
        self.assertEqual(sp.main(["rebuild", os.path.join(self.shared, self.mid, "G15FL"), "-o", out]), 0)
        self.assertTrue(os.path.isfile(os.path.join(out, "meet.json")))
        self.assertTrue(os.path.isfile(os.path.join(out, "pulls", "entries.json")))
        one = os.path.join(self.tmp, "one.json")
        self.assertEqual(sp.main(["rebuild", os.path.join(self.shared, self.mid, "G15FL"), "-o", one]), 0)
        with open(one, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["meet"]["id"], self.mid)          # the meet alone

    def test_our_old_single_file_is_rewritten_split(self):
        # 2026-10-10: after the update nothing had changed, so the Drive copy
        # stayed the single file; it's republished split on the next round
        mdir = os.path.join(self.shared, self.mid)
        a = ms.MeetSync(ms.DirStore(self.store), ms.FolderTransport(self.shared), "G15FL",
                        os.path.join(self.tmp, "a-state"))
        a.sync_once()
        os.makedirs(os.path.join(self.tmp, "old"))
        shutil.move(os.path.join(mdir, "G15FL"), os.path.join(self.tmp, "old", "G15FL"))
        with open(os.path.join(mdir, "G15FL.json.gz"), "wb") as f:      # as the old code left it
            f.write(ms.pack(self.whole))
        self.assertEqual(a.sync_once()["published"], [self.mid])
        self.assertEqual(os.listdir(mdir), ["G15FL"])
        self.assertEqual(a.sync_once()["published"], [])


if __name__ == "__main__":
    unittest.main()
